#!/usr/bin/env python3
"""
Halaxy MCP server.

Tools: list_invoices(date), list_appointments(date), list_practitioners(),
list_invoices_by_payer(payer_name), list_referrals(flag).
Talks to Halaxy using the OAuth
client_credentials flow. Matches the permissions enabled on the
"ClaudeMCPLimited" Halaxy API key (Appointments -> Retrieve, Invoices &
Payments -> Retrieve / Retrieve Fees, Practitioners -> Retrieve,
Claims & Referrals -> Retrieve Claim). "Patients -> Retrieve" is
deliberately left OFF on the API key - this server never calls Halaxy's
Patient endpoint at all (see below), so it doesn't need that scope
enabled, and it's a real, easy-to-verify guarantee (in the Halaxy UI,
not just in this code) that no patient data beyond a bare ID ever could
leave this server, even in a future bug.

"Retrieve Claim" is Halaxy's plain-English label for read access to the
FHIR `Coverage` resource - a patient's health-fund/insurer/employer
membership and billing-target details (which entity gets invoiced for
their sessions), not clinical claim data and not Medicare submission
status.

If the API key's scopes don't cover what a tool needs, Halaxy responds
with a 401/403 or an OperationOutcome error - `_halaxy_get` detects that
and raises HalaxyPermissionError with a clear message, rather than
letting a caller like `_fetch_all` silently read the missing "entry" key
as zero results (which would otherwise look identical to "no invoices
today" instead of "this API key can't see invoices at all").

Patient data leaving this server is deliberately reduced to a bare,
non-identifying Halaxy ID (`patient_id`) - no name, phone, email, DOB,
address, gender, patient status, or any other field Halaxy's Patient
resource carries. This isn't only about DOB/address: for a psychology
practice, a person's *name* tied to a session is itself health
information under the Privacy Act (a person receiving psychological
treatment) - so no tool here returns a patient's name, there is no
`find_patient`-style lookup-by-name tool, and no tool answers "who is
this session/invoice/referral with" - only "what patient_id, and what
non-identifying facts (funding_type, session_mode, referral limits,
etc)". Invoice titles and session descriptions - free text Halaxy lets
staff put a client's name into - are scrubbed for the same reason (see
`_invoice_payer_name` and `list_appointments`'s handling of session
descriptions).

Transport: stdio (default, for Claude Desktop's local subprocess model)
or http (MCP_TRANSPORT=http - for Docker/the Raspberry Pi deployment,
reachable by remote MCP clients like Claude's custom connectors or
Microsoft 365 Copilot). See README.md's "Docker / HTTP transport"
section.

The HTTP transport's OAuth implementation (_SimpleOAuthProvider) was
security-reviewed by Igal Belkin (GrowInsight), who found and reported a
consent-phishing/token-theft chain via open Dynamic Client Registration,
a reflected XSS on the login page, and an `mcp` version pin that could
resolve to a build where token exchange 500s - credited here since the
fixes below are a direct response to that review, not found internally.
"""

import html
import json
import os
import re
import secrets
import time
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

import httpx
from dotenv import load_dotenv
from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    OAuthAuthorizationServerProvider,
    RefreshToken,
    RegistrationError,
    construct_redirect_uri,
)
from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions
from mcp.server.fastmcp import FastMCP
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from pydantic import AnyHttpUrl
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response

# Load .env from next to this script, not from whatever directory the
# process happens to be launched from (Claude Desktop won't necessarily
# set the working directory to this folder).
load_dotenv(Path(__file__).resolve().parent / ".env")

HALAXY_API_BASE = os.environ.get("HALAXY_API_BASE", "https://au-api.halaxy.com/main").rstrip("/")
HALAXY_CLIENT_ID = os.environ.get("HALAXY_CLIENT_ID")
HALAXY_CLIENT_SECRET = os.environ.get("HALAXY_CLIENT_SECRET")

# Transport selection - stdio (default, for Claude Desktop's local subprocess
# model) vs. http (for the Docker/Pi deployment, reachable by remote MCP
# clients like Claude's custom connectors or Microsoft 365 Copilot).
MCP_TRANSPORT = os.environ.get("MCP_TRANSPORT", "stdio")
MCP_HOST = os.environ.get("MCP_HOST", "127.0.0.1")
MCP_PORT = int(os.environ.get("MCP_PORT", "8000"))

# The public HTTPS URL remote MCP clients actually reach this server at
# (behind Caddy on the real deployment) - NOT the internal MCP_HOST/
# MCP_PORT the container binds to. Used as the OAuth issuer URL and to
# build the login page's callback URL. For local testing without TLS,
# e.g. http://127.0.0.1:8000.
MCP_PUBLIC_URL = os.environ.get("MCP_PUBLIC_URL", f"http://{MCP_HOST}:{MCP_PORT}")

# Confirmed live (screenshot of Claude's real "Add custom connector"
# dialog): it requires a genuine OAuth flow - there's no field anywhere
# to paste in a static bearer token - so MCP_LOGIN_USERNAME/PASSWORD gate
# a real (if minimal) login page, not a header value. One shared login
# for the practice, not per-person; see the OAuth provider below for why
# that's a reasonable choice here.
MCP_LOGIN_USERNAME = os.environ.get("MCP_LOGIN_USERNAME")
MCP_LOGIN_PASSWORD = os.environ.get("MCP_LOGIN_PASSWORD")

# Hostnames a newly-*registered* OAuth client's redirect_uri is allowed to
# point at, comma-separated (e.g. "claude.ai,chatgpt.com"). Dynamic Client
# Registration is intentionally open (Claude/Copilot/ChatGPT all need to
# self-register with no prior setup), which otherwise lets anyone register
# their own client with an attacker-controlled redirect_uri and phish the
# shared login password via a legitimate-looking sign-in link - reported
# by Igal Belkin (GrowInsight). Leaving this unset keeps the old, more
# permissive behaviour (a warning is logged at startup) rather than
# silently locking out a deployment that hasn't set it yet; set it to
# close the hole for real. The safest way to populate it is from your own
# already-connected clients' redirect_uris, e.g.:
#   python3 -c "import json; d=json.load(open('oauth_state.json')); \
#     print(','.join(sorted({c['redirect_uris'][0].split('/')[2] for c in d['clients'].values()})))"
MCP_ALLOWED_REDIRECT_URI_HOSTS = {
    host.strip() for host in os.environ.get("MCP_ALLOWED_REDIRECT_URI_HOSTS", "").split(",") if host.strip()
}

# How long a /login link is valid for after /authorize issues it, and how
# many failed sign-in attempts a single source IP gets before being
# locked out - see _SimpleOAuthProvider for how both are used. Neither
# was enforced at all before Igal Belkin's review.
LOGIN_STATE_TTL_SECONDS = 10 * 60
LOGIN_MAX_ATTEMPTS = 5
LOGIN_LOCKOUT_SECONDS = 15 * 60

# Access tokens are short-lived on purpose (limits how long a leaked one is
# useful for) - but without a refresh token, an MCP client has no way to
# renew one silently, and has to send the human back through the full
# interactive /login page every single time it expires. Refresh tokens
# fix that: a client exchanges its (rotated on every use, per OAuth 2.1's
# requirement for public clients) refresh token for a fresh access token
# in the background, so a human only sees the login page again once every
# REFRESH_TOKEN_TTL_SECONDS, not every ACCESS_TOKEN_TTL_SECONDS.
ACCESS_TOKEN_TTL_SECONDS = 60 * 60
REFRESH_TOKEN_TTL_SECONDS = 90 * 24 * 60 * 60

# Where registered OAuth clients + issued access tokens are persisted, so
# a docker-compose restart/rebuild (which happens on every code update)
# doesn't silently invalidate every connected client's session - confirmed
# live this was a real, recurring problem before this existed. In Docker
# this should point at a path backed by a volume (docker-compose.pi.yml
# mounts one at /data), so it survives `docker compose up --build`, not
# just a plain container restart.
MCP_OAUTH_STATE_FILE = os.environ.get(
    "MCP_OAUTH_STATE_FILE", str(Path(__file__).resolve().parent / "oauth_state.json")
)

PRACTICE_TIMEZONE = ZoneInfo("Australia/Sydney")

# Simple in-memory token cache. Halaxy access tokens are valid for
# 15 minutes; we refresh a little early to be safe.
_token_cache = {"access_token": None, "expires_at": 0}


def _get_access_token() -> str:
    """Fetch (or reuse a cached) Halaxy OAuth access token."""
    if not HALAXY_CLIENT_ID or not HALAXY_CLIENT_SECRET:
        raise RuntimeError(
            "Missing Halaxy credentials - set HALAXY_CLIENT_ID and HALAXY_CLIENT_SECRET in .env"
        )

    if _token_cache["access_token"] and time.time() < _token_cache["expires_at"]:
        return _token_cache["access_token"]

    response = httpx.post(
        f"{HALAXY_API_BASE}/oauth/token",
        json={
            "grant_type": "client_credentials",
            "client_id": HALAXY_CLIENT_ID,
            "client_secret": HALAXY_CLIENT_SECRET,
        },
        headers={"Accept": "application/fhir+json", "Content-Type": "application/json"},
        timeout=15,
    )
    data = response.json()

    if response.status_code != 200 or "access_token" not in data:
        raise RuntimeError(
            f"Failed to obtain Halaxy access token: "
            f"{data.get('error_description') or data.get('message') or data}"
        )

    # expires_in is reported as 3600s, but Halaxy's docs say tokens are
    # actually only valid 15 minutes - trust the shorter figure, minus a
    # small safety margin so we never call Halaxy with a stale token.
    _token_cache["access_token"] = data["access_token"]
    _token_cache["expires_at"] = time.time() + min(data.get("expires_in", 900), 900) - 60
    return _token_cache["access_token"]


class HalaxyPermissionError(RuntimeError):
    """Raised when Halaxy rejects a request because the API key's scope doesn't allow it."""


def _is_expired_token_error(data: dict) -> bool:
    return data.get("resourceType") == "OperationOutcome" and data.get("issue", [{}])[0].get("code") == "expired"


def _operation_outcome_message(data: dict) -> str:
    """Best-effort human-readable text out of a FHIR OperationOutcome's issue list."""
    issue = data.get("issue", [{}])[0]
    return (
        issue.get("diagnostics")
        or issue.get("details", {}).get("text")
        or issue.get("code")
        or "no further detail in Halaxy's response"
    )


def _halaxy_get(path: str, params: dict) -> dict:
    """GET a Halaxy FHIR endpoint with the current access token, retrying once on an expired token.

    Raises HalaxyPermissionError on a 401/403 or an OperationOutcome error
    response, instead of silently handing that back as if it were real
    data - a caller like `_fetch_all` would otherwise read an
    OperationOutcome's absent "entry" key as zero results, quietly
    reporting "no invoices"/"no appointments" instead of the actual
    problem (most commonly: this scope isn't enabled on the API key).
    """
    token = _get_access_token()
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    url = f"{HALAXY_API_BASE}/{path}"

    response = httpx.get(url, params=params, headers=headers, timeout=20)
    data = response.json()

    if _is_expired_token_error(data):
        _token_cache["access_token"] = None
        token = _get_access_token()
        headers["Authorization"] = f"Bearer {token}"
        response = httpx.get(url, params=params, headers=headers, timeout=20)
        data = response.json()

    if response.status_code in (401, 403) or (
        data.get("resourceType") == "OperationOutcome"
        and any(issue.get("severity") == "error" for issue in data.get("issue", []))
    ):
        resource = path.split("/", 1)[0]
        raise HalaxyPermissionError(
            f"Halaxy denied this request for {resource} (HTTP {response.status_code}): "
            f"{_operation_outcome_message(data)}. This usually means the API key doesn't have "
            f"the matching scope enabled - check Settings -> API Keys in Halaxy."
        )

    return data


MCP_OAUTH_SCOPE = "mcp"

# Applied to every HTML response the OAuth provider renders itself (the
# login page). Reported by Igal Belkin (GrowInsight): the login page had
# no headers at all, so a successful reflected-XSS injection there ran
# with no mitigation whatsoever. CSP alone would have neutralised the
# specific injected <script> he demonstrated; the other two are standard
# defense-in-depth for a page that takes a password.
LOGIN_PAGE_HEADERS = {
    "Content-Security-Policy": "default-src 'self'; frame-ancestors 'none'",
    "X-Frame-Options": "DENY",
    "X-Content-Type-Options": "nosniff",
}


class _SimpleOAuthProvider(OAuthAuthorizationServerProvider[AuthorizationCode, RefreshToken, AccessToken]):
    """Minimal self-contained OAuth 2.1 authorization server for this MCP server.

    Adapted from Anthropic's own reference pattern
    (modelcontextprotocol/python-sdk, examples/servers/simple-auth, the
    "legacy" combined authorization-server-plus-resource-server mode) for
    a single-tenant server with one shared login, rather than a plain
    bearer token: confirmed live (screenshot of the real "Add custom
    connector" dialog) that Claude's connector setup requires a genuine
    OAuth flow with Dynamic Client Registration - there's no field to
    paste a static token into at all.

    Persisted to a small JSON file (MCP_OAUTH_STATE_FILE) so a
    docker-compose restart/rebuild - which happens every time this code
    is updated - doesn't invalidate every connected client's session,
    forcing Claude/Copilot/ChatGPT to all reconnect. Confirmed live this
    was a real, recurring problem before persistence was added: a Pi
    redeploy silently wiped every registered client and access token,
    and the next tool call from an already-connected client came back
    as an opaque "permission required" error with no obvious cause.
    `state_mapping` (mid-flow authorize state, not yet a real client/
    token) is deliberately NOT persisted - it's only relevant for the
    seconds between hitting /authorize and completing /login, so a
    restart in that exact window just means retrying the connection,
    which is an acceptable edge case in exchange for not persisting
    throwaway state forever.

    "Signing in" is one shared username/password (MCP_LOGIN_USERNAME/
    PASSWORD) gating a plain login page, not a per-person identity system
    - reasonable for a two-person practice; swapping the single
    username/password check in `handle_login` for a lookup against a
    small dict of accounts would extend this to per-person logins if that
    's ever wanted instead.

    Security-reviewed by Igal Belkin (GrowInsight), who reported (and
    reproduced end-to-end) that open Dynamic Client Registration plus a
    login page showing nothing about who's requesting access let an
    attacker self-register a client with their own redirect_uri, send a
    legitimate-looking sign-in link, and walk away with the shared
    password's authorisation code once a victim signed in - a full
    consent-phishing/token-theft chain, since one token here reads the
    whole practice's Halaxy data. Also reported: a reflected XSS on this
    same login page via the unescaped `state` parameter (worse in
    combination with the above - the injected page IS the password
    prompt), and an `mcp` version pin that can resolve to a build where
    `subject=` on `AuthorizationCode`/`AccessToken` doesn't exist yet,
    500ing the token exchange. Fixed here: `state_mapping` is now keyed
    by a server-generated value (never the client-supplied `state`, which
    is echoed back untouched instead - see `authorize`), the login page
    is escaped and shows the requesting client's name and redirect
    target (see `login_page_html`), new client registrations are
    restricted to MCP_ALLOWED_REDIRECT_URI_HOSTS when that's configured
    (see `register_client`), login attempts are throttled per source IP
    (see `handle_login_callback`), and `requirements.txt` now floors on
    the `mcp` version that actually has `subject`.

    Also issues real refresh tokens now (rotated on every use, per OAuth
    2.1's requirement for public clients - see `exchange_refresh_token`).
    Originally this raised NotImplementedError, on the theory that
    reconnecting occasionally was an acceptable simplification - in
    practice that meant every client had to redo the full interactive
    /login page every time its 1-hour access token expired, not just
    reconnect once. A refresh token lets a client renew silently in the
    background instead, so a human only sees the login page again once
    every REFRESH_TOKEN_TTL_SECONDS (90 days), not every
    ACCESS_TOKEN_TTL_SECONDS (1 hour).
    """

    def __init__(self):
        self.clients: dict[str, OAuthClientInformationFull] = {}
        self.auth_codes: dict[str, AuthorizationCode] = {}
        self.tokens: dict[str, AccessToken] = {}
        self.refresh_tokens: dict[str, RefreshToken] = {}
        self.state_mapping: dict[str, dict[str, str | None]] = {}
        # Per-source-IP login throttle - deliberately NOT persisted, same
        # reasoning as state_mapping: a restart clearing it is a fine
        # tradeoff for not carrying throttle state forever, and it's only
        # ever consulted within a single process's uptime anyway.
        self._login_attempts: dict[str, dict[str, float | int]] = {}
        self._load()

    def _load(self) -> None:
        """Restore clients/codes/tokens from disk, if a state file exists - dropping anything already expired."""
        path = Path(MCP_OAUTH_STATE_FILE)
        if not path.exists():
            return
        try:
            data = json.loads(path.read_text())
        except (json.JSONDecodeError, OSError):
            return

        now = time.time()
        self.clients = {
            client_id: OAuthClientInformationFull.model_validate(c)
            for client_id, c in data.get("clients", {}).items()
        }
        self.auth_codes = {
            code: AuthorizationCode.model_validate(c)
            for code, c in data.get("auth_codes", {}).items()
            if c["expires_at"] > now
        }
        self.tokens = {
            token: AccessToken.model_validate(t)
            for token, t in data.get("tokens", {}).items()
            if not t["expires_at"] or t["expires_at"] > now
        }
        self.refresh_tokens = {
            token: RefreshToken.model_validate(t)
            for token, t in data.get("refresh_tokens", {}).items()
            if not t["expires_at"] or t["expires_at"] > now
        }

    def _save(self) -> None:
        """Write clients/codes/tokens to disk - called after every mutation, not just on shutdown.

        This file holds live access tokens - as sensitive as any bearer
        credential - so it's written with 0600 permissions, and via a
        temp-file-then-rename (atomic on POSIX) so a crash mid-write
        can never leave a half-written, corrupt state file behind.
        """
        path = Path(MCP_OAUTH_STATE_FILE)
        data = {
            "clients": {client_id: c.model_dump(mode="json") for client_id, c in self.clients.items()},
            "auth_codes": {code: c.model_dump(mode="json") for code, c in self.auth_codes.items()},
            "tokens": {token: t.model_dump(mode="json") for token, t in self.tokens.items()},
            "refresh_tokens": {token: t.model_dump(mode="json") for token, t in self.refresh_tokens.items()},
        }
        tmp_path = path.with_suffix(".tmp")
        tmp_path.write_text(json.dumps(data))
        tmp_path.chmod(0o600)
        tmp_path.replace(path)

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        return self.clients.get(client_id)

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        if not client_info.client_id:
            raise ValueError("No client_id provided")
        if MCP_ALLOWED_REDIRECT_URI_HOSTS:
            for redirect_uri in client_info.redirect_uris:
                host = urlparse(str(redirect_uri)).hostname
                if host not in MCP_ALLOWED_REDIRECT_URI_HOSTS:
                    # RFC 7591 error code - the DCR handler turns this into
                    # the correct 400 response for us (see mcp.server.auth
                    # .handlers.register). Reported by Igal Belkin
                    # (GrowInsight): open registration with no allowlist
                    # let an attacker register a client pointed at their
                    # own redirect_uri, then phish the shared login
                    # password via a legitimate-looking sign-in link.
                    raise RegistrationError(
                        error="invalid_client_metadata",
                        error_description=f"redirect_uri host {host!r} is not in MCP_ALLOWED_REDIRECT_URI_HOSTS",
                    )
        self.clients[client_info.client_id] = client_info
        self._save()

    async def authorize(self, client: OAuthClientInformationFull, params: AuthorizationParams) -> str:
        """Called as part of Claude's /authorize request - send it on to our own login page."""
        # The dict key here MUST be a value only this server ever chose -
        # never the client-supplied `params.state`. Reported by Igal
        # Belkin (GrowInsight): keying by the client's own state let an
        # attacker pre-seed an entry the victim's flow could inherit if
        # it happened to reuse the same value. The client's own `state`
        # (which MUST be echoed back untouched in the final redirect, per
        # the OAuth spec) is carried through as `client_state` instead of
        # ever being used for a lookup.
        login_state = secrets.token_hex(16)
        self.state_mapping[login_state] = {
            "redirect_uri": str(params.redirect_uri),
            "code_challenge": params.code_challenge,
            "redirect_uri_provided_explicitly": str(params.redirect_uri_provided_explicitly),
            "client_id": client.client_id,
            "resource": params.resource,
            "client_state": params.state,
            "expires_at": time.time() + LOGIN_STATE_TTL_SECONDS,
        }
        return f"{MCP_PUBLIC_URL}/login?state={login_state}"

    async def load_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: str
    ) -> AuthorizationCode | None:
        return self.auth_codes.get(authorization_code)

    async def exchange_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: AuthorizationCode
    ) -> OAuthToken:
        if authorization_code.code not in self.auth_codes:
            raise ValueError("Invalid authorization code")
        if not client.client_id:
            raise ValueError("No client_id provided")

        token = f"mcp_{secrets.token_hex(32)}"
        self.tokens[token] = AccessToken(
            token=token,
            client_id=client.client_id,
            scopes=authorization_code.scopes,
            expires_at=int(time.time()) + ACCESS_TOKEN_TTL_SECONDS,
            resource=authorization_code.resource,
            subject=authorization_code.subject,
        )
        refresh_token = f"mcp_refresh_{secrets.token_hex(32)}"
        self.refresh_tokens[refresh_token] = RefreshToken(
            token=refresh_token,
            client_id=client.client_id,
            scopes=authorization_code.scopes,
            expires_at=int(time.time()) + REFRESH_TOKEN_TTL_SECONDS,
            subject=authorization_code.subject,
        )
        del self.auth_codes[authorization_code.code]
        self._save()

        return OAuthToken(
            access_token=token,
            token_type="Bearer",
            expires_in=ACCESS_TOKEN_TTL_SECONDS,
            scope=" ".join(authorization_code.scopes),
            refresh_token=refresh_token,
        )

    async def load_access_token(self, token: str) -> AccessToken | None:
        access_token = self.tokens.get(token)
        if not access_token:
            return None
        if access_token.expires_at and access_token.expires_at < time.time():
            del self.tokens[token]
            self._save()
            return None
        return access_token

    async def load_refresh_token(self, client: OAuthClientInformationFull, refresh_token: str) -> RefreshToken | None:
        token = self.refresh_tokens.get(refresh_token)
        if not token or token.client_id != client.client_id:
            return None
        if token.expires_at and token.expires_at < time.time():
            del self.refresh_tokens[refresh_token]
            self._save()
            return None
        return token

    async def exchange_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: RefreshToken, scopes: list[str]
    ) -> OAuthToken:
        if refresh_token.token not in self.refresh_tokens:
            raise ValueError("Invalid refresh token")

        # Rotate on every use - the old refresh token is dead the moment a
        # new one is issued, per OAuth 2.1's requirement for public clients
        # (this server doesn't distinguish public/confidential clients, so
        # applies it uniformly). Limits how long a stolen refresh token
        # stays useful: the legitimate client's next real refresh silently
        # invalidates a copy an attacker made, rather than both living on
        # side by side indefinitely.
        del self.refresh_tokens[refresh_token.token]

        new_access_token = f"mcp_{secrets.token_hex(32)}"
        self.tokens[new_access_token] = AccessToken(
            token=new_access_token,
            client_id=client.client_id,
            scopes=scopes or refresh_token.scopes,
            expires_at=int(time.time()) + ACCESS_TOKEN_TTL_SECONDS,
            subject=refresh_token.subject,
        )
        new_refresh_token = f"mcp_refresh_{secrets.token_hex(32)}"
        self.refresh_tokens[new_refresh_token] = RefreshToken(
            token=new_refresh_token,
            client_id=client.client_id,
            scopes=scopes or refresh_token.scopes,
            expires_at=int(time.time()) + REFRESH_TOKEN_TTL_SECONDS,
            subject=refresh_token.subject,
        )
        self._save()

        return OAuthToken(
            access_token=new_access_token,
            token_type="Bearer",
            expires_in=ACCESS_TOKEN_TTL_SECONDS,
            scope=" ".join(scopes or refresh_token.scopes),
            refresh_token=new_refresh_token,
        )

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        if token.token in self.tokens:
            del self.tokens[token.token]
            self._save()
        elif token.token in self.refresh_tokens:
            del self.refresh_tokens[token.token]
            self._save()

    def login_page_html(self, state: str, client_name: str, redirect_uri: str) -> str:
        """Render the login/consent page - `state`, `client_name`, and `redirect_uri` are all
        HTML-escaped by the caller (see `handle_login_page`) before reaching here, not here itself,
        so every call site is forced to escape rather than relying on this function to remember to.

        Showing `client_name`/`redirect_uri` is the consent step reported missing by Igal Belkin
        (GrowInsight): previously this page said only "Halaxy MCP" regardless of which client (or
        attacker-registered pseudo-client) initiated the flow, or where the resulting code would be
        sent - so there was nothing for a human to actually check before typing the shared password.
        """
        return f"""
        <!DOCTYPE html>
        <html>
        <head><title>Halaxy MCP - Sign in</title>
        <style>
            body {{ font-family: system-ui, sans-serif; max-width: 420px; margin: 80px auto; padding: 0 20px; }}
            .form-group {{ margin-bottom: 15px; }}
            input {{ width: 100%; padding: 8px; margin-top: 5px; box-sizing: border-box; }}
            button {{ background-color: #4CAF50; color: white; padding: 10px 15px; border: none; cursor: pointer; }}
            .consent {{ background: #f4f4f4; border-radius: 6px; padding: 12px 16px; margin-bottom: 20px; font-size: 14px; word-break: break-all; }}
        </style>
        </head>
        <body>
            <h2>Halaxy MCP</h2>
            <div class="consent">
                <strong>{client_name}</strong> is requesting access.<br>
                After signing in, you'll be sent to:<br>{redirect_uri}
            </div>
            <form action="{MCP_PUBLIC_URL}/login/callback" method="post">
                <input type="hidden" name="state" value="{state}">
                <div class="form-group">
                    <label>Username</label>
                    <input type="text" name="username" required autofocus>
                </div>
                <div class="form-group">
                    <label>Password</label>
                    <input type="password" name="password" required>
                </div>
                <button type="submit">Sign in &amp; authorize</button>
            </form>
        </body>
        </html>
        """

    def _client_ip(self, request: Request) -> str:
        """Best-effort source IP for login-attempt throttling (see `handle_login_callback`).

        Trusts the first hop of `X-Forwarded-For` because of this deployment's specific network
        shape, not in general: `docker-compose.pi.yml` never publishes this app's own port to the
        host, so Caddy is the only thing that can reach it at all, and Caddy sets this header itself
        on every request it proxies - nothing outside that path can forge it here. Falls back to the
        raw socket peer for local/non-Docker testing, where there's no proxy in front at all.
        """
        forwarded = request.headers.get("x-forwarded-for")
        if forwarded:
            return forwarded.split(",")[0].strip()
        return request.client.host if request.client else "unknown"

    def _check_login_rate_limit(self, ip: str) -> None:
        """Raise 429 if `ip` has LOGIN_MAX_ATTEMPTS+ failed sign-ins within LOGIN_LOCKOUT_SECONDS.

        Reported by Igal Belkin (GrowInsight): `POST /login/callback` had no throttle of any kind,
        so the single shared practice password was online-bruteforceable against an internet-facing
        server with nothing to slow an attacker down.
        """
        attempts = self._login_attempts.get(ip)
        if not attempts:
            return
        now = time.time()
        failures = [t for t in attempts["failures"] if now - t < LOGIN_LOCKOUT_SECONDS]
        attempts["failures"] = failures
        if len(failures) >= LOGIN_MAX_ATTEMPTS:
            raise HTTPException(429, f"Too many failed sign-in attempts - try again in {LOGIN_LOCKOUT_SECONDS // 60} minutes")

    async def handle_login_page(self, request: Request) -> Response:
        state = request.query_params.get("state")
        if not state:
            raise HTTPException(400, "Missing state parameter")

        state_data = self.state_mapping.get(state)
        if not state_data or state_data["expires_at"] < time.time():
            # Deliberately checked here too, not just in handle_login_callback: an unrecognised
            # `state` used to still render a normal-looking login page (see login_page_html's
            # docstring), which is exactly the blind-prompt behaviour Igal Belkin's XSS PoC relied
            # on to reach an arbitrary GET parameter in the first place.
            raise HTTPException(400, "Invalid or expired state parameter - try connecting again")

        client = self.clients.get(state_data["client_id"])
        client_name = client.client_name if client and client.client_name else state_data["client_id"]
        return HTMLResponse(
            self.login_page_html(
                html.escape(state),
                html.escape(client_name),
                html.escape(state_data["redirect_uri"]),
            ),
            headers=LOGIN_PAGE_HEADERS,
        )

    async def handle_login_callback(self, request: Request) -> Response:
        ip = self._client_ip(request)
        self._check_login_rate_limit(ip)

        form = await request.form()
        username, password, state = form.get("username"), form.get("password"), form.get("state")
        if not isinstance(username, str) or not isinstance(password, str) or not isinstance(state, str):
            raise HTTPException(400, "Missing username, password, or state parameter")

        state_data = self.state_mapping.get(state)
        if not state_data or state_data["expires_at"] < time.time():
            raise HTTPException(400, "Invalid or expired state parameter - try connecting again")

        if not MCP_LOGIN_USERNAME or not MCP_LOGIN_PASSWORD:
            raise HTTPException(500, "Server has no MCP_LOGIN_USERNAME/PASSWORD configured")
        if not (secrets.compare_digest(username, MCP_LOGIN_USERNAME) and secrets.compare_digest(password, MCP_LOGIN_PASSWORD)):
            self._login_attempts.setdefault(ip, {"failures": []})["failures"].append(time.time())
            raise HTTPException(401, "Invalid credentials")
        self._login_attempts.pop(ip, None)

        redirect_uri = state_data["redirect_uri"]
        code_challenge = state_data["code_challenge"]
        client_id = state_data["client_id"]
        client_state = state_data.get("client_state")
        assert redirect_uri is not None and client_id is not None

        new_code = f"mcp_{secrets.token_hex(16)}"
        self.auth_codes[new_code] = AuthorizationCode(
            code=new_code,
            client_id=client_id,
            redirect_uri=AnyHttpUrl(redirect_uri),
            redirect_uri_provided_explicitly=state_data["redirect_uri_provided_explicitly"] == "True",
            expires_at=time.time() + 300,
            scopes=[MCP_OAUTH_SCOPE],
            code_challenge=code_challenge,
            resource=state_data.get("resource"),
            subject=username,
        )
        del self.state_mapping[state]
        self._save()

        # `state` echoed back to the client here MUST be the client's own original value
        # (`client_state`), never our internal `state_mapping` key - see `authorize`.
        return RedirectResponse(
            url=construct_redirect_uri(redirect_uri, code=new_code, state=client_state), status_code=302
        )


_oauth_provider = _SimpleOAuthProvider()

mcp = FastMCP(
    "halaxy-mcp",
    host=MCP_HOST,
    port=MCP_PORT,
    auth_server_provider=_oauth_provider,
    auth=AuthSettings(
        issuer_url=AnyHttpUrl(MCP_PUBLIC_URL),
        client_registration_options=ClientRegistrationOptions(
            enabled=True,
            valid_scopes=[MCP_OAUTH_SCOPE],
            default_scopes=[MCP_OAUTH_SCOPE],
        ),
        required_scopes=[MCP_OAUTH_SCOPE],
        # No separate resource server - this server is both AS and RS
        # ("legacy" combined mode), matching the reference pattern above.
        resource_server_url=None,
    ),
)


@mcp.custom_route("/health", methods=["GET"])
async def _health(request):
    """Unauthenticated liveness check - for Docker/Caddy, not a real tool."""
    from starlette.responses import PlainTextResponse

    return PlainTextResponse("ok")


@mcp.custom_route("/login", methods=["GET"])
async def _login_page(request: Request) -> Response:
    return await _oauth_provider.handle_login_page(request)


@mcp.custom_route("/login/callback", methods=["POST"])
async def _login_callback(request: Request) -> Response:
    return await _oauth_provider.handle_login_callback(request)

# Halaxy's Invoice endpoint has NO search parameter for the invoice's own
# "date" field - confirmed live against Halaxy's CapabilityStatement
# (GET /metadata). The only date-ish filters it supports are `created`
# (when the record was first created) and `_lastUpdated` (when it was
# last touched, which includes unrelated things like a payment being
# applied to a months-old invoice - not a reliable proxy for "today").
#
# So: pull invoices created within a generous lookback window (comfortably
# longer than any realistic gap between an invoice being raised and the
# date it's dated for), then filter for an exact match on the invoice's
# own `date` field ourselves. Validated live: correctly found all 5
# invoices genuinely dated a specific day out of a ~200-invoice window.
INVOICE_LOOKBACK_DAYS = 45
MAX_PAGES = 10


def _fetch_all(resource: str, params: dict) -> list[dict]:
    """Fetch every resource of `resource` type matching `params`, following pagination."""
    results: list[dict] = []
    page = 1

    while page <= MAX_PAGES:
        data = _halaxy_get(resource, {**params, "_count": 100, "page": page})
        results.extend(
            entry["resource"]
            for entry in data.get("entry", [])
            if entry.get("resource", {}).get("resourceType") == resource
        )

        last_page = page
        for link in data.get("link", []):
            if link.get("relation") == "last":
                match = re.search(r"page=(\d+)", link.get("url", ""))
                if match:
                    last_page = int(match.group(1))

        if page >= last_page:
            break
        page += 1

    return results


def _fetch_invoices_created_since(since_date: str) -> list[dict]:
    """Fetch every Invoice resource created on/after `since_date` (YYYY-MM-DD), across pages."""
    return _fetch_all("Invoice", {"created": f"ge{since_date}"})


def _invoice_payer_name(inv: dict) -> str | None:
    """Invoice.title, unless it's identifying a patient - Halaxy titles patient-billed invoices with
    the patient's own name, which is exactly what this server must never return (see module
    docstring). Org-billed invoices (insurer/employer) are unaffected - that name isn't patient data.
    """
    if inv.get("recipient", {}).get("type") == "Patient":
        return None
    return inv.get("title")


def _invoice_funding_type(inv: dict) -> str:
    """Who's actually billed for this invoice, without naming them: "self" (the patient themselves)
    or "organisation" (an insurer/employer, billed via a Coverage record - see
    `_get_patient_insurer_coverage`). Lets a caller answer "is this session self-funded or
    insurer-funded" without ever needing the payer's name.
    """
    return "organisation" if inv.get("recipient", {}).get("type") == "Organization" else "self"


@mcp.tool()
def list_invoices(date: str | None = None) -> str:
    """List Halaxy invoices dated a given day.

    Args:
        date: Date in YYYY-MM-DD format. If omitted, defaults to today
            (Australia/Sydney time). Invoices are normally dated the
            same day as the client session they relate to, though this
            isn't guaranteed - a small number are dated a day or two
            after they're actually raised (e.g. pre-billed sessions).

    Returns:
        JSON with the target date and the matching invoices (id, payer
        name, patient_id, funding_type, and amounts), for Claude to read
        and summarise. `patient_id` is only populated - as a bare Halaxy
        ID, never a name (see module docstring) - when the invoice's
        recipient is an actual Patient. `funding_type` is `"self"` when
        the patient themselves is billed, `"organisation"` when an
        insurer/employer is - this is the safe way to answer "is this
        self-funded or insurer-funded" without a name. `payer_name` (from
        the invoice's own title) is only returned when `funding_type` is
        `"organisation"` - Halaxy titles patient-billed invoices with the
        patient's own name, so that title is withheld for a "self"
        invoice.
    """
    if date is None:
        date = datetime.now(PRACTICE_TIMEZONE).strftime("%Y-%m-%d")

    since = (datetime.strptime(date, "%Y-%m-%d") - timedelta(days=INVOICE_LOOKBACK_DAYS)).strftime("%Y-%m-%d")
    candidates = _fetch_invoices_created_since(since)
    matches = [inv for inv in candidates if inv.get("date") == date]

    summary = [
        {
            "id": inv.get("id"),
            "payer_name": _invoice_payer_name(inv),
            "funding_type": _invoice_funding_type(inv),
            "patient_id": (
                _ref_id(inv["recipient"]["reference"])
                if inv.get("recipient", {}).get("type") == "Patient"
                else None
            ),
            "status": inv.get("status"),
            "date": inv.get("date"),
            "totalGross": inv.get("totalGross", {}).get("value"),
            "totalPaid": inv.get("totalPaid", {}).get("value"),
            "totalBalance": inv.get("totalBalance", {}).get("value"),
        }
        for inv in matches
    ]

    return json.dumps(
        {"queried_date": date, "invoice_count": len(summary), "invoices": summary},
        indent=2,
    )


INVOICE_EXTENSION_URL = "https://terminology.halaxy.com/StructureDefinition/appointment-participant-invoice"


def _ref_id(reference: str | None) -> str | None:
    """Pull the trailing ID off a FHIR reference URL, e.g. '.../Patient/149719437' -> '149719437'."""
    return reference.rsplit("/", 1)[-1] if reference else None


def _preferred_name(name_list: list[dict]) -> dict | None:
    """Pick the best HumanName entry off a resource - a Patient can have several

    (e.g. Halaxy stores a "usual" given-name-only entry alongside a fuller
    "official" one) - confirmed live: naively taking name_list[0] silently
    picked the given-name-only entry for at least one real patient.
    Prefers "official", then any entry with a family name or text, else
    just the first entry.
    """
    if not name_list:
        return None
    return next(
        (n for n in name_list if n.get("use") == "official"),
        next((n for n in name_list if n.get("family") or n.get("text")), name_list[0]),
    )


def _human_name(name_list: list[dict]) -> str | None:
    """Render a FHIR HumanName list (Practitioner.name / Patient.name) as a plain display string."""
    name = _preferred_name(name_list)
    if not name:
        return None
    if name.get("text"):
        return name["text"]
    full = " ".join([*name.get("given", []), name.get("family", "")]).strip()
    return full or None


# Practitioner/PractitionerRole data is the practice's staff roster - it
# changes far less often than invoices or appointments, so it's cached
# in-memory rather than re-fetched (2 paginated Halaxy calls) on every
# list_appointments call.
PRACTITIONER_CACHE_TTL_SECONDS = 6 * 60 * 60
_practitioner_cache = {"role_id_to_info": None, "expires_at": 0}


def _get_practitioner_role_names() -> dict[str, dict]:
    """Map PractitionerRole ID -> {name, practitioner_id, active}, cached."""
    if _practitioner_cache["role_id_to_info"] is not None and time.time() < _practitioner_cache["expires_at"]:
        return _practitioner_cache["role_id_to_info"]

    names_by_practitioner_id = {
        practitioner["id"]: _human_name(practitioner.get("name", []))
        for practitioner in _fetch_all("Practitioner", {})
    }

    role_id_to_info = {}
    for role in _fetch_all("PractitionerRole", {}):
        practitioner_id = _ref_id(role.get("practitioner", {}).get("reference"))
        role_id_to_info[role["id"]] = {
            "name": names_by_practitioner_id.get(practitioner_id),
            "practitioner_id": practitioner_id,
            "active": role.get("active"),
        }

    _practitioner_cache["role_id_to_info"] = role_id_to_info
    _practitioner_cache["expires_at"] = time.time() + PRACTITIONER_CACHE_TTL_SECONDS
    return role_id_to_info


@mcp.tool()
def list_practitioners() -> str:
    """List clinical staff (practitioners), each with their PractitionerRole ID and name.

    The PractitionerRole ID is what appointments reference (see
    `practitioner_role_id` / `practitioner_name` in list_appointments) -
    use this tool if you need the full staff list, or to double check a
    name Claude has already resolved.

    Returns:
        JSON list of practitioners: {practitioner_role_id, name, active}.
    """
    role_id_to_info = _get_practitioner_role_names()
    practitioners = [
        {"practitioner_role_id": role_id, "name": info["name"], "active": info["active"]}
        for role_id, info in role_id_to_info.items()
    ]
    practitioners.sort(key=lambda p: p["name"] or "")

    return json.dumps(
        {"practitioner_count": len(practitioners), "practitioners": practitioners},
        indent=2,
    )


# This server never calls Halaxy's Patient endpoint at all, and doesn't
# need the "Patients -> Retrieve" scope on the API key - every place a
# patient needs identifying is already carrying the patient's ID on the
# resource being read (Appointment.participant, Invoice.recipient,
# Referral.subject), and the ID is *all* this server ever surfaces (see
# module docstring for why even a name is withheld). There is nothing
# else to reduce/allowlist here, unlike the fields on a fetched Patient
# resource - there's simply no Patient resource fetched in the first
# place.
APPOINTMENT_PARTICIPANT_STATUS_EXTENSION_URL = (
    "https://terminology.halaxy.com/StructureDefinition/appointment-participant-status"
)
CANCELLED_PARTICIPANT_STATUS = "cancelled"


def _is_appointment_cancelled(appointment: dict) -> bool:
    """Whether Halaxy has this appointment marked as cancelled.

    Confirmed live (this was a real bug - a cancelled session was showing
    up as a normal upcoming appointment): the top-level `cancellationReason`
    field is NOT a reliable signal by itself - many real cancelled
    appointments don't have it set. The authoritative signal is the
    Patient participant's "appointment-participant-status" modifierExtension
    (not `extension` - Halaxy puts this one under modifierExtension) being
    "cancelled". Checking both, since `cancellationReason` can still appear
    on appointments the participant-status extension doesn't cover (no
    Patient participant at all, e.g. a cancelled meeting/blocker).
    """
    if appointment.get("cancellationReason"):
        return True

    for participant in appointment.get("participant", []):
        if participant.get("actor", {}).get("type") != "Patient":
            continue
        for ext in participant.get("modifierExtension", []):
            if (
                ext.get("url") == APPOINTMENT_PARTICIPANT_STATUS_EXTENSION_URL
                and ext.get("valueCoding", {}).get("code") == CANCELLED_PARTICIPANT_STATUS
            ):
                return True

    return False


def _appointment_participant_refs(appointment: dict) -> dict:
    """Pull the patient ID, practitioner-role ID, and (if billed) linked invoice ID off an Appointment."""
    patient_id = None
    practitioner_role_id = None
    invoice_id = None

    for participant in appointment.get("participant", []):
        actor = participant.get("actor", {})
        actor_type = actor.get("type")

        if actor_type == "Patient":
            patient_id = _ref_id(actor.get("reference"))
            for ext in participant.get("extension", []):
                if ext.get("url") == INVOICE_EXTENSION_URL:
                    invoice_id = _ref_id(ext.get("valueReference", {}).get("reference"))
        elif actor_type == "PractitionerRole":
            practitioner_role_id = _ref_id(actor.get("reference"))

    return {"patient_id": patient_id, "practitioner_role_id": practitioner_role_id, "invoice_id": invoice_id}


def _healthcare_service_id(appointment: dict) -> str | None:
    """Pull the HealthcareService ID off an Appointment's supportingInformation, if present."""
    for ref in appointment.get("supportingInformation", []):
        if ref.get("type") == "HealthcareService":
            return _ref_id(ref.get("reference"))
    return None


# The HealthcareService an appointment is booked against is how Halaxy
# models F2F vs. Telehealth (confirmed live: HealthcareService.name is
# literally "F2F" or "Telehealth" in this account) - a small, effectively
# static set of service types, so cached indefinitely per process rather
# than re-fetched per appointment.
_healthcare_service_name_cache: dict[str, str | None] = {}


def _get_healthcare_service_name(service_id: str) -> str | None:
    if service_id not in _healthcare_service_name_cache:
        service = _halaxy_get(f"HealthcareService/{service_id}", {})
        _healthcare_service_name_cache[service_id] = (
            service.get("name") if service.get("resourceType") == "HealthcareService" else None
        )
    return _healthcare_service_name_cache[service_id]


def _search_organizations_by_name(name: str) -> list[dict]:
    """Search Halaxy Organization records by name (funders, insurers, employers, clinics)."""
    data = _halaxy_get("Organization", {"name": name})
    return [
        entry["resource"]
        for entry in data.get("entry", [])
        if entry.get("resource", {}).get("resourceType") == "Organization"
    ]


# Organization records show up both as a generic funder category on
# Coverage.payor (e.g. "Medicare", "WorkCover") and as the specific
# claims-manager entity actually billed on Invoice.recipient / Coverage's
# coverage-organisation extension (e.g. a specific insurer's name) - a
# small, effectively static set, so cached indefinitely per process.
_organization_name_cache: dict[str, str | None] = {}


def _get_organization_name(org_id: str) -> str | None:
    if org_id not in _organization_name_cache:
        org = _halaxy_get(f"Organization/{org_id}", {})
        _organization_name_cache[org_id] = org.get("name") if org.get("resourceType") == "Organization" else None
    return _organization_name_cache[org_id]


COVERAGE_PAYER_EXTENSION_URL = "https://terminology.halaxy.com/StructureDefinition/coverage-payer"
COVERAGE_ORGANISATION_EXTENSION_URL = "https://terminology.halaxy.com/StructureDefinition/coverage-organisation"
COVERAGE_EMPLOYER_EXTENSION_URL = "https://au-api.halaxy.com/main/FunderType/employer"

# Confirmed live: Coverage's "coverage-payer" extension is a coded field -
# "29" ("Patient") means the patient themselves is billed for the
# session; "30" ("Organisation (new invoice)") means a third party
# (insurer/employer, named on the "coverage-organisation" extension) is
# billed instead. This is what actually determines who ends up as an
# invoice's recipient - a patient can have several Coverage records
# (Medicare, private health fund, a specific employer's workers' comp
# claim, etc.), so this looks for the one flagged this way.
COVERAGE_PAYER_ORGANISATION_CODE = "30"

PATIENT_INSURER_CACHE_TTL_SECONDS = 6 * 60 * 60
_patient_insurer_cache: dict[str, dict] = {}


def _get_patient_insurer_coverage(patient_id: str) -> dict | None:
    """The patient's active insurer/employer-billed Coverage, if Halaxy has one on file.

    Returns None if the patient is billed directly, or has no such
    Coverage at all.
    """
    cached = _patient_insurer_cache.get(patient_id)
    if cached is not None and time.time() < cached["expires_at"]:
        return cached["coverage"]

    coverage_result = None
    for coverage in _fetch_all("Coverage", {"beneficiary": f"Patient/{patient_id}"}):
        if coverage.get("status") != "active":
            continue
        extensions = coverage.get("extension", [])
        payer_code = next(
            (
                ext.get("valueCoding", {}).get("code")
                for ext in extensions
                if ext.get("url") == COVERAGE_PAYER_EXTENSION_URL
            ),
            None,
        )
        if payer_code != COVERAGE_PAYER_ORGANISATION_CODE:
            continue
        insurer_org_id = next(
            (
                _ref_id(ext.get("valueReference", {}).get("reference"))
                for ext in extensions
                if ext.get("url") == COVERAGE_ORGANISATION_EXTENSION_URL
            ),
            None,
        )
        employer = next(
            (ext.get("valueString") for ext in extensions if ext.get("url") == COVERAGE_EMPLOYER_EXTENSION_URL),
            None,
        )
        coverage_result = {
            "insurer_name": _get_organization_name(insurer_org_id) if insurer_org_id else None,
            "employer": employer,
        }
        break

    _patient_insurer_cache[patient_id] = {
        "coverage": coverage_result,
        "expires_at": time.time() + PATIENT_INSURER_CACHE_TTL_SECONDS,
    }
    return coverage_result


_practitioner_name_cache: dict[str, str | None] = {}


def _get_practitioner_name(practitioner_id: str) -> str | None:
    """Resolve a Practitioner (not PractitionerRole) ID to a name - referrals reference both types."""
    if practitioner_id not in _practitioner_name_cache:
        practitioner = _halaxy_get(f"Practitioner/{practitioner_id}", {})
        _practitioner_name_cache[practitioner_id] = (
            _human_name(practitioner.get("name", [])) if practitioner.get("resourceType") == "Practitioner" else None
        )
    return _practitioner_name_cache[practitioner_id]


def _resolve_actor_name(actor_ref: dict, practitioner_role_names: dict) -> str | None:
    """Resolve a FHIR reference that's either a Practitioner or a PractitionerRole to a display name."""
    ref_id = _ref_id((actor_ref or {}).get("reference"))
    if not ref_id:
        return None
    if actor_ref.get("type") == "PractitionerRole":
        return practitioner_role_names.get(ref_id, {}).get("name")
    return _get_practitioner_name(ref_id)


_referral_definition_name_cache: dict[str, str | None] = {}


def _get_referral_definition_name(definition_id: str) -> str | None:
    """The referral type's name, e.g. "Medicare: MHTP Referral" - a small, effectively static set."""
    if definition_id not in _referral_definition_name_cache:
        definition = _halaxy_get(f"ReferralDefinition/{definition_id}", {})
        _referral_definition_name_cache[definition_id] = (
            definition.get("name") if definition.get("resourceType") == "ReferralDefinition" else None
        )
    return _referral_definition_name_cache[definition_id]


# How soon a referral's expiry counts as "coming up" - confirmed live this
# has to be computed here, not trusted from Halaxy's own `active` flag:
# Halaxy doesn't appear to auto-flip a referral to inactive just because
# its period has lapsed.
REFERRAL_EXPIRING_SOON_DAYS = 30


def _referral_flags(referral: dict, today) -> list[str]:
    flags = []
    limit_quantity = referral.get("limitQuantity", {})
    total = limit_quantity.get("quantity", {}).get("value")
    used = limit_quantity.get("quantityUsed")
    if total is not None and used is not None and used >= total:
        flags.append("over_limit")

    end = referral.get("period", {}).get("end")
    if end:
        end_date = datetime.strptime(end, "%Y-%m-%d").date()
        if end_date < today:
            flags.append("expired")
        elif (end_date - today).days <= REFERRAL_EXPIRING_SOON_DAYS:
            flags.append("expiring_soon")

    return flags


def _referral_summary(referral: dict, practitioner_role_names: dict) -> dict:
    """Reduce a Referral resource to what's useful for tracking session/dollar limits and expiry.

    Confirmed live: Halaxy models a Medicare Mental Health Treatment Plan
    (and similar - DVA, WorkCover) as a Referral with a `limitQuantity`
    (sessions authorized + already used) and/or a `limitMoney` (dollar cap
    + used) - "sessions_remaining" is total minus used, computed here
    since Halaxy doesn't return it directly. A patient can have more than
    one simultaneously active Referral (seen live - e.g. one per
    referred-to practitioner), so this deliberately doesn't try to pick
    "the" one; callers get the full list.
    """
    limit_quantity = referral.get("limitQuantity", {})
    total = limit_quantity.get("quantity", {}).get("value")
    used = limit_quantity.get("quantityUsed")
    limit_money = referral.get("limitMoney", {})
    referral_definition_id = _ref_id(referral.get("referralDefinition", {}).get("reference"))

    return {
        "id": referral.get("id"),
        "referral_type": _get_referral_definition_name(referral_definition_id) if referral_definition_id else None,
        "referring_practitioner": _resolve_actor_name(referral.get("requester"), practitioner_role_names),
        "referred_to_practitioner": _resolve_actor_name(referral.get("performer"), practitioner_role_names),
        "period_start": referral.get("period", {}).get("start"),
        "period_end": referral.get("period", {}).get("end"),
        "sessions_total": total,
        "sessions_used": used,
        "sessions_remaining": (total - used) if total is not None and used is not None else None,
        "amount_total": limit_money.get("amount", {}).get("value"),
        "amount_used": limit_money.get("amountUsed"),
        # Some referrals in this account have no referralDefinition/requester
        # at all - just a free-text comment naming the referrer (confirmed
        # live) - surfaced as the only clue available in that case.
        "comment": referral.get("comment"),
        "flags": _referral_flags(referral, datetime.now(PRACTICE_TIMEZONE).date()),
    }


PATIENT_REFERRAL_CACHE_TTL_SECONDS = 6 * 60 * 60
_patient_referral_cache: dict[str, dict] = {}


def _get_patient_active_referrals(patient_id: str) -> list[dict]:
    """The patient's active Referral(s) (Medicare MHTP, DVA, WorkCover, etc.), most recent first."""
    cached = _patient_referral_cache.get(patient_id)
    if cached is not None and time.time() < cached["expires_at"]:
        return cached["referrals"]

    practitioner_role_names = _get_practitioner_role_names()
    referrals = [
        _referral_summary(referral, practitioner_role_names)
        for referral in _fetch_all("Referral", {"subject": f"Patient/{patient_id}"})
        if referral.get("active")
    ]
    referrals.sort(key=lambda referral: referral["period_start"] or "", reverse=True)

    _patient_referral_cache[patient_id] = {
        "referrals": referrals,
        "expires_at": time.time() + PATIENT_REFERRAL_CACHE_TTL_SECONDS,
    }
    return referrals


# Keywords someone might type into a meeting's description to mark it as
# blocked/non-working time. NOT exhaustive, and NOT how Halaxy's own
# calendar actually labels these in practice - confirmed live, real
# examples of Halaxy's UI showing "BREAK" and "Nat Leaves at 5pm" both
# came back from the API with a completely blank description. So this
# keyword match will rarely fire on its own; see `_meeting_availability_hint`.
NON_WORKING_MEETING_KEYWORDS = (
    "BREAK",
    "BLOCK",
    "BLOCKED",
    "LUNCH",
    "HOLIDAY",
    "LEAVE",
    "OOO",
    "OUT OF OFFICE",
    "UNAVAILABLE",
)


def _meeting_availability_hint(description: str | None) -> dict:
    """Best-effort GUESS at whether a meeting is a non-working-time blocker - not a real Halaxy field.

    This is not backed by any Halaxy data at all - it's pattern-matching
    on free text, offered as a hint for the MCP client, not a claim about
    actual availability. Confirmed live: Halaxy's calendar UI displays
    generic-looking blocker titles ("BREAK", "Nat Leaves at 5pm") for
    meetings whose `description` the API returns as completely blank -
    there's no Halaxy field anywhere that names or categorises a meeting
    (see the API investigation notes). So a blank description is weak but
    real evidence (it's what Halaxy's own known blockers look like via
    this API); an explicit keyword match is slightly stronger evidence,
    for the rarer case where staff typed something recognisable.

    IMPORTANT: `likely_non_working: True` does NOT mean the time is free
    to book a client into. This server has no access to real Halaxy
    availability data (the Schedule/Slot resources, which model that,
    aren't used here) - it only means "this meeting looks like it might
    not represent real client-facing work", nothing more.
    """
    text = (description or "").strip()
    if not text:
        return {
            "likely_non_working": True,
            "confidence": "low",
            "reason": "blank description - matches the pattern seen for known blockers, but not conclusive",
        }
    if any(keyword in text.upper() for keyword in NON_WORKING_MEETING_KEYWORDS):
        return {
            "likely_non_working": True,
            "confidence": "medium",
            "reason": "description contains a blocker-like keyword (e.g. \"break\", \"block\", \"leave\")",
        }
    return {"likely_non_working": False, "confidence": None, "reason": None}


@mcp.tool()
def list_appointments(date: str | None = None, appointment_type: str | None = None) -> str:
    """List appointments for a given day, each tagged as a client "session" or a "meeting".

    Halaxy links an appointment to its invoice directly (an
    "appointment-participant-invoice" reference on the appointment itself)
    - this is the authoritative link, more reliable than matching invoices
    up by date. Not every appointment has one yet (nothing to bill, or not
    billed yet); those come back with invoice: null.

    "session" vs "meeting": confirmed live against real data - an
    appointment booked against a billable client always has a "Patient"
    participant on it; a blocker, reminder, internal meeting, or phone-call
    note never does (it only has the practitioner). That presence/absence
    is the authoritative signal used here, not the free-text `description`
    field (which is inconsistent - sometimes a session counter like "3/8",
    sometimes a note like "Kelly to call Chris on ...", sometimes empty).

    Practitioners are resolved via this API key's "Practitioners"
    permission. Patients are never looked up at all - `patient_id` is
    just the bare Halaxy ID already carried on the appointment itself, no
    Patient endpoint call involved, and no name/phone/email/DOB/anything
    else about who that ID belongs to is ever returned (see module
    docstring for why even a name is withheld). `description` is never
    returned at all, for a session OR a meeting - staff put client names
    and clinical detail into both (confirmed live: a real meeting titled
    "Case conference with <client's name>"), so having no Patient
    participant doesn't make a meeting's free text safe to pass through
    either. `availability_hint` (meetings only, below) is the one thing
    still derived from that text - only its non-identifying verdict is
    returned, never the text itself.

    IMPORTANT for answering identity-style questions ("who is my 2pm
    with", "who's <time>'s session"): this tool cannot tell you who a
    session is with, on purpose, and no other tool in this server can
    either. Answer using only what this tool actually returns - e.g. "I
    can't tell you who that's with (patient identity isn't exposed
    through this server), but it's a self-funded F2F session at 2pm" -
    rather than guessing, inferring a name from context, or treating
    `patient_id` as if it were identifying information to relay.

    Args:
        date: Date in YYYY-MM-DD format. Defaults to today (Australia/Sydney).
        appointment_type: Optionally filter to just "session" or "meeting".
            Omit to return both.

    Sessions also carry a `session_mode` ("F2F" or "Telehealth" in this
    account) - Halaxy models this via the HealthcareService the
    appointment is booked against (Appointment.supportingInformation),
    not the description field. Meetings have no HealthcareService, so
    this is always null for them.

    A session's linked `invoice` (see above) carries `funding_type` -
    `"self"` if the patient themselves is billed, `"organisation"` if an
    insurer/employer is - so "is this session self-funded or
    insurer-funded" is answerable without a name. `payer_name` on that
    invoice is only populated when `funding_type` is `"organisation"`
    (Halaxy titles patient-billed invoices with the patient's own name,
    so that's withheld for a "self" invoice).

    A session with no invoice yet also carries `awaiting_insurer_invoice`
    - populated (with the insurer/employer name) only when the patient
    has an active Coverage on file flagged "billed to organisation"
    (Halaxy's own mechanism for routing an invoice to an insurer/employer
    instead of the patient) - i.e. a session that's expected to be billed
    to a specific insurer, but hasn't been yet. Use `list_invoices_by_payer`
    to check that insurer's actual invoice history.

    Sessions also carry `referrals` - the patient's active Referral(s)
    (e.g. a Medicare Mental Health Treatment Plan), each with sessions/
    dollars authorized vs. used, expiry, and `flags` ("over_limit",
    "expiring_soon", "expired"). Usually one, but a patient can have more
    than one active at once (e.g. one per referred-to practitioner) - an
    empty list means no active referral on file, not necessarily that
    they're self-referred (some funding types don't use Referral at all).

    Cancelled appointments are excluded entirely (not counted, not
    returned) - confirmed live this was previously a real bug: Halaxy's
    `Appointment?date=eq...` search includes cancelled appointments by
    default, with no indication in the fields this server used to look at
    that they'd been cancelled, so one showed up looking like a completely
    normal upcoming session. `cancelled_count` reports how many were
    excluded, so the exclusion is visible rather than silent.

    Meetings also carry `availability_hint` - a best-effort GUESS at
    whether the meeting represents non-working/blocked time (e.g. a
    break), based on pattern-matching the `description` text. This is
    NOT a real Halaxy field and NOT availability data - `likely_non_working:
    true` must never be read as "this time is free to book a client
    into". Halaxy's own calendar shows generic blocker titles ("BREAK",
    "Nat Leaves at 5pm") for meetings the API returns with a completely
    blank description - confirmed live - so there's no reliable way to
    detect these from this API at all; this is just the closest
    approximation available, offered as a hint.

    Returns:
        JSON with the target date, each appointment's type, time (never
        the raw `description`, for either type - see above), session
        mode, patient_id (bare Halaxy ID, never a name, or null for a
        meeting), practitioner name (and role ID), linked invoice details
        including
        funding_type (or null), awaiting_insurer_invoice, referrals, and
        (meetings only) availability_hint - plus `cancelled_count`
        (excluded from `appointments` itself).
    """
    if date is None:
        date = datetime.now(PRACTICE_TIMEZONE).strftime("%Y-%m-%d")
    if appointment_type not in (None, "session", "meeting"):
        raise ValueError('appointment_type must be "session", "meeting", or omitted')

    data = _halaxy_get("Appointment", {"date": f"eq{date}", "_count": 100})
    all_appointments = [
        entry["resource"]
        for entry in data.get("entry", [])
        if entry.get("resource", {}).get("resourceType") == "Appointment"
    ]
    appointments = [a for a in all_appointments if not _is_appointment_cancelled(a)]
    cancelled_count = len(all_appointments) - len(appointments)

    practitioner_role_names = _get_practitioner_role_names()

    parsed = []
    invoice_ids_to_resolve = set()
    for appt in appointments:
        refs = _appointment_participant_refs(appt)
        this_type = "session" if refs["patient_id"] else "meeting"
        if appointment_type and this_type != appointment_type:
            continue
        parsed.append(
            {
                "appointment_id": appt.get("id"),
                "appointment_type": this_type,
                "start": appt.get("start"),
                "end": appt.get("end"),
                # Raw `description` text - for sessions AND meetings - is
                # never returned, full stop. It's free text staff can (and
                # do) put a client's name or clinical detail into - a real
                # example seen live: a meeting titled "Case conference with
                # <client's name>". Meetings having no Patient participant
                # doesn't mean their description can't still name a client,
                # so "not sure" here means "withhold", not "guess it's
                # fine". `_meeting_availability_hint` below still reads the
                # raw text internally to classify a meeting - that's the
                # one place description content is used at all, and only
                # its (non-identifying) verdict is ever returned.
                "session_mode": (
                    _get_healthcare_service_name(service_id)
                    if (service_id := _healthcare_service_id(appt))
                    else None
                ),
                "patient_id": refs["patient_id"],
                "practitioner_role_id": refs["practitioner_role_id"],
                "practitioner_name": practitioner_role_names.get(refs["practitioner_role_id"], {}).get("name"),
                "invoice_id": refs["invoice_id"],
                "availability_hint": (
                    _meeting_availability_hint(appt.get("description")) if this_type == "meeting" else None
                ),
            }
        )
        if refs["invoice_id"]:
            invoice_ids_to_resolve.add(refs["invoice_id"])

    invoices_by_id = {}
    for invoice_id in invoice_ids_to_resolve:
        invoice = _halaxy_get(f"Invoice/{invoice_id}", {})
        if invoice.get("resourceType") == "Invoice":
            invoices_by_id[invoice_id] = {
                "payer_name": _invoice_payer_name(invoice),
                "funding_type": _invoice_funding_type(invoice),
                "status": invoice.get("status"),
                "date": invoice.get("date"),
                "totalGross": invoice.get("totalGross", {}).get("value"),
                "totalPaid": invoice.get("totalPaid", {}).get("value"),
                "totalBalance": invoice.get("totalBalance", {}).get("value"),
            }

    for item in parsed:
        invoice = invoices_by_id.get(item.pop("invoice_id"))
        item["invoice"] = invoice
        item["awaiting_insurer_invoice"] = (
            _get_patient_insurer_coverage(item["patient_id"])
            if invoice is None and item["appointment_type"] == "session" and item["patient_id"]
            else None
        )
        item["referrals"] = (
            _get_patient_active_referrals(item["patient_id"])
            if item["appointment_type"] == "session" and item["patient_id"]
            else []
        )

    return json.dumps(
        {
            "queried_date": date,
            "appointment_count": len(parsed),
            "cancelled_count": cancelled_count,
            "appointments": parsed,
        },
        indent=2,
    )


@mcp.tool()
def list_invoices_by_payer(payer_name: str) -> str:
    """Search every invoice ever billed to a specific insurer/employer/organisation.

    Unlike list_invoices, this isn't tied to a single day or a lookback
    window - it searches Halaxy's invoice history directly via
    Invoice.recipient (confirmed live: this is a real, unbounded Halaxy
    search parameter, not client-side filtering). That matters
    specifically for insurer-billed invoices: they're exactly the ones
    most likely to have an old `created` date relative to the session
    they relate to (a workers' comp claim can be set up months before the
    session it ends up billed for), which is what makes list_invoices's
    45-day lookback window miss them. This tool has no such blind spot.

    Args:
        payer_name: Insurer/employer/organisation name to search for (e.g.
            "Acme Insurance", "WorkCover", "Medicare") - matched against
            Halaxy's own Organization name search, not a raw text search
            over invoices, so it can find every organisation record with a
            matching or similar name (there can be more than one - e.g. a
            generic funder-category record and the specific claims-manager
            entity actually billed) and search all of them.

    Returns:
        JSON with the organisation(s) matched and every invoice billed to
        any of them (id, status, date, amounts) - or an empty list if no
        organisation matches that name.
    """
    orgs = _search_organizations_by_name(payer_name)

    invoices = []
    for org in orgs:
        for inv in _fetch_all("Invoice", {"recipient": f"Organization/{org['id']}"}):
            invoices.append(
                {
                    "id": inv.get("id"),
                    "payer_name": inv.get("title"),
                    "status": inv.get("status"),
                    "date": inv.get("date"),
                    "totalGross": inv.get("totalGross", {}).get("value"),
                    "totalPaid": inv.get("totalPaid", {}).get("value"),
                    "totalBalance": inv.get("totalBalance", {}).get("value"),
                }
            )
    invoices.sort(key=lambda inv: inv["date"] or "", reverse=True)

    return json.dumps(
        {
            "payer_name": payer_name,
            "matched_organizations": [{"id": org["id"], "name": org.get("name")} for org in orgs],
            "invoice_count": len(invoices),
            "invoices": invoices,
        },
        indent=2,
    )


@mcp.tool()
def list_referrals(flag: str | None = None) -> str:
    """List every active Referral in the practice, with session/dollar limits, expiry, and status flags.

    A Referral is Halaxy's model for a GP/other referral authorizing a
    set number of sessions and/or dollars under a funding scheme - most
    commonly a Medicare Mental Health Treatment Plan (6 sessions to
    start), but also DVA, WorkCover, etc. Confirmed live against real
    referrals in this account.

    Each referral carries `flags`, computed here (not trusted from
    Halaxy's own `active` field, which doesn't appear to auto-update on
    expiry):
      - "over_limit" - sessions used >= sessions authorized
      - "expiring_soon" - period ends within 30 days
      - "expired" - period has already ended

    Use this for practice-wide sweeps ("how many referrals are about to
    run out of sessions", "which referrals are expiring soon") - answer
    with counts/lists keyed by `patient_id` and referral details, not a
    patient's name (this server never returns one - see module
    docstring). For a specific client's current session count while
    looking at their appointments, see the `referrals` field on
    `list_appointments`.

    Args:
        flag: Optionally filter to just one of "over_limit", "expiring_soon",
            or "expired". Omit to return every active referral.

    Returns:
        JSON list of referrals, each with patient_id (a bare Halaxy ID,
        never a name), referral type, referring/referred-to practitioner,
        period, sessions/dollars total vs. used vs. remaining, and flags.
    """
    if flag not in (None, "over_limit", "expiring_soon", "expired"):
        raise ValueError('flag must be "over_limit", "expiring_soon", "expired", or omitted')

    practitioner_role_names = _get_practitioner_role_names()
    referrals = []
    for referral in _fetch_all("Referral", {}):
        if not referral.get("active"):
            continue
        summary = _referral_summary(referral, practitioner_role_names)
        if flag and flag not in summary["flags"]:
            continue
        summary["patient_id"] = _ref_id(referral.get("subject", {}).get("reference"))
        referrals.append(summary)

    referrals.sort(key=lambda r: r["period_end"] or "")

    return json.dumps(
        {"flag": flag, "referral_count": len(referrals), "referrals": referrals},
        indent=2,
    )


def _run_http() -> None:
    if not MCP_LOGIN_USERNAME or not MCP_LOGIN_PASSWORD:
        raise RuntimeError(
            "MCP_TRANSPORT=http requires MCP_LOGIN_USERNAME and MCP_LOGIN_PASSWORD to be set - "
            "the credentials the OAuth login page checks. Pick a username and a strong password "
            "and set them in the environment."
        )

    import uvicorn

    uvicorn.run(mcp.streamable_http_app(), host=MCP_HOST, port=MCP_PORT)


if __name__ == "__main__":
    if MCP_TRANSPORT == "http":
        _run_http()
    else:
        mcp.run()
