# halaxy-mcp

An [MCP](https://modelcontextprotocol.io) server for the [Halaxy](https://www.halaxy.com/) practice-management API, written in Python. It lets an MCP client (Claude, GitHub Copilot, etc.) answer questions like "what's on my calendar today", "which of today's appointments haven't been invoiced yet", or "what invoices are outstanding with a given insurer", by talking to your own Halaxy account.

This is a small, single-tenant tool built for one practice's own use, not a general-purpose Halaxy SDK - see [What it deliberately doesn't do](#what-it-deliberately-doesnt-do) below.

**⚠️ Read [DISCLAIMER.md](DISCLAIMER.md) before using this with real patient data.** Provided as-is, no warranty, no support commitment - and using it doesn't make *your* deployment compliant with privacy law. That's on you to assess, not this codebase.

## Tools

All five carry the standard [MCP tool annotation hints](https://modelcontextprotocol.io/docs/concepts/tools#tool-annotations) - `readOnlyHint: true`, `destructiveHint: false`, `idempotentHint: true`, `openWorldHint: false` - since every one is a read-only Halaxy query, safe to call repeatedly, scoped to one practice's own account.

- **`list_invoices(date)`** - invoices dated a given day (defaults to today). Each invoice has `patient_id` (a bare Halaxy ID, only present when the payer is an actual patient), a `funding_type` (`"self"` or `"organisation"` - see [Patient data](#patient-data) below), and a `payer_name` (only present when `funding_type` is `"organisation"`).
- **`list_appointments(date, appointment_type)`** - appointments for a given day, each tagged `"session"` (a real client appointment) or `"meeting"` (a blocker/reminder/internal note - anything with no linked patient). Sessions also carry:
  - `session_mode` - `"F2F"` or `"Telehealth"`, resolved from the HealthcareService the appointment is booked against
  - `patient_id` - a bare Halaxy ID, nothing else (see [Patient data](#patient-data) below)
  - `invoice` - the linked invoice, if one's been raised, via Halaxy's direct appointment→invoice reference, including `funding_type`
  - `awaiting_insurer_invoice` - populated only when there's no invoice yet *and* the patient has an active Coverage on file flagged "billed to an organisation" - a session expected to be billed to an insurer/employer but not yet
  - `referrals` - the patient's active Referral(s) (see `list_referrals` below)

  Cancelled appointments are excluded entirely (`cancelled_count` reports how many) - detected via the Patient participant's `appointment-participant-status` **modifierExtension** (not `extension`), since the top-level `cancellationReason` field alone isn't reliable.

  Meetings also carry `availability_hint` - a best-effort *guess* at whether the meeting is non-working/blocked time, matching keywords in `description` or a blank description. **This is not a real Halaxy field and not availability data** - `likely_non_working: true` must never be read as "free to book a client into".

  Meetings also carry `meeting_category` - `"case_conference"` when the description *starts with* "Case Conference" or "CC with" (a prefix check, not a substring search), else `null`. This answers "does the practitioner have a case conference today" without ever exposing who it's with.
- **`list_practitioners()`** - clinical staff, each with their PractitionerRole ID and name.
- **`list_invoices_by_payer(payer_name)`** - every invoice ever billed to a specific insurer/employer/organisation (e.g. "Acme Insurance"), not tied to any date - searches Halaxy's `Invoice?recipient=` directly, so it doesn't have `list_invoices`'s lookback-window blind spot (see below).
- **`list_referrals(flag)`** - every active Referral in the practice - Halaxy's model for a GP/other referral authorizing a set number of sessions and/or dollars under a funding scheme (Medicare Mental Health Treatment Plan, DVA, WorkCover, etc). Each carries a `patient_id`, `sessions_total`/`sessions_used`/`sessions_remaining`, `amount_total`/`amount_used`, expiry, and computed `flags`: `"over_limit"`, `"expiring_soon"` (within 30 days), `"expired"`. Optionally filter to just one flag.

## Required Halaxy API key scopes

Create an API key in Halaxy (Settings → API Keys) with whichever of these you need - the server degrades gracefully if a scope is off, it'll just fail on the tools that need it:

| Scope (as labelled in Halaxy's UI) | Used by |
|---|---|
| Appointments → Retrieve | `list_appointments` |
| Invoices & Payments → Retrieve, Retrieve Fees | `list_invoices`, `list_invoices_by_payer` |
| Practitioners → Retrieve | `list_practitioners`, practitioner names in `list_appointments` |
| Claims & Referrals → Retrieve Claim | `awaiting_insurer_invoice`, `list_invoices_by_payer` (Halaxy's label for read access to the FHIR `Coverage` resource) |
| Claims & Referrals → Retrieve Referral | `list_referrals`, `referrals` in `list_appointments` |

**`Patients → Retrieve` is deliberately left off** - this server never calls Halaxy's `Patient` endpoint at all (see [Patient data](#patient-data) below), so it doesn't need that scope enabled. Leaving it off is a real, Halaxy-side guarantee that no patient data beyond a bare ID could leave this server, not just a code-level one.

Example of what this looks like in Halaxy's own API key scope screen:

![Halaxy API key scopes screen](docs/api-access-required.jpg)

## Patient data

This server never returns a patient's name, phone number, email, DOB, address, gender, or any other identifying field - only a bare, opaque `patient_id`, the same ID already carried on the appointment/invoice/referral resource itself. There's no Patient resource fetched at all, so there's nothing beyond an ID to leak.

This is a deliberate privacy-law decision, not just "no DOB/address/gender": for a psychology practice, a client's *name* tied to a session is itself health information under Australia's Privacy Act, and health service providers don't get the small-business exemption other businesses can rely on regardless of size. Disclosing that to a third-party AI vendor's servers needs its own lawful basis - a legal/policy question for the practice running this, not something this server should quietly decide by exposing the data. Concretely:

- No tool returns a patient's name, phone, or email, and no tool answers "who is this session/invoice/referral with" - only a `patient_id` plus non-identifying facts (`funding_type`, `session_mode`, referral limits).
- `description` is withheld for both sessions and meetings, full stop - it's free text staff can put a client's name into either way. `funding_type` (`"self"` vs. `"organisation"`) gives you the self-funded/insurer-funded distinction without a name.
- Every tool's own docstring instructs the calling model on how to answer identity-style questions ("who is my 2pm with") without guessing - though that's guidance for the model, not a substitute for there simply being no name in the data to relay.

Widening this deliberately (re-enabling `Patients → Retrieve` and what `halaxy_mcp.py` returns) is a decision to make on purpose, not something that should happen by accident.

**Clinical/session notes are not retrievable through this API at all, for any key or scope** - Halaxy's `DocumentReference` resource supports `create`/`patch` only, no read. A whole-API limitation, not something this server chooses not to expose.

## Referrals and session limits

Halaxy models a GP Mental Health Treatment Plan (and similar - DVA, WorkCover) as a `Referral` linked to a `ReferralDefinition` (the referral *type*, which carries the session/dollar cap). `sessions_remaining` isn't returned by Halaxy directly; it's computed here as `sessions_total - sessions_used`.

Worth knowing if you extend this further:
- A patient can have more than one simultaneously-active Referral - this server returns all of them rather than guessing "the" one.
- `sessions_used` can exceed `sessions_total` (Medicare doesn't hard-stop bookings at the cap) - that's what `"over_limit"` is for.
- Some Referral records have no structured type/referrer, just a free-text `comment` - surfaced as-is.
- Halaxy's own `active` field doesn't auto-flip to false once a Referral's period lapses - `"expired"`/`"expiring_soon"` are computed from `period.end` instead.

## If a scope isn't enabled

If a scope is missing, Halaxy responds with a 401/403 or an `OperationOutcome` error - the server raises a clear `HalaxyPermissionError` (naming the resource, the HTTP status, and Halaxy's own error text) rather than silently treating that as "zero results", so a missing scope and a genuinely empty result don't look identical to the MCP client.

## Install

Requires Python 3.10+.

```bash
git clone https://github.com/ryanhunt/halaxy-mcp.git
cd halaxy-mcp
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# then edit .env with your Halaxy API key's client_id/client_secret
```

Sanity-check it runs:

```bash
source .venv/bin/activate
python3 halaxy_mcp.py
```

It won't print anything and will just sit there - that's correct, it's waiting for an MCP client to talk to it over stdin/stdout. Ctrl+C to stop it.

## Wiring it into an MCP client

All of these spawn the same script as a local subprocess and talk to it over stdio - no network port, no separate deployment. Use the **full, absolute path** to the `.venv`'s Python and to `halaxy_mcp.py` in every case.

**Claude Desktop** - add to `claude_desktop_config.json` (`~/Library/Application Support/Claude/claude_desktop_config.json` on macOS):

```json
{
  "mcpServers": {
    "halaxy-mcp": {
      "command": "/absolute/path/to/halaxy-mcp/.venv/bin/python3",
      "args": ["/absolute/path/to/halaxy-mcp/halaxy_mcp.py"]
    }
  }
}
```

Fully quit and reopen the app afterwards (not just close the window).

**VS Code (GitHub Copilot)** - add `.vscode/mcp.json` in a workspace:

```json
{
  "servers": {
    "halaxy-mcp": {
      "type": "stdio",
      "command": "/absolute/path/to/halaxy-mcp/.venv/bin/python3",
      "args": ["/absolute/path/to/halaxy-mcp/halaxy_mcp.py"]
    }
  }
}
```

**GitHub Copilot CLI** - add to `~/.copilot/mcp-config.json` (or run `/mcp add` inside the CLI):

```json
{
  "mcpServers": {
    "halaxy-mcp": {
      "type": "local",
      "command": "/absolute/path/to/halaxy-mcp/.venv/bin/python3",
      "args": ["/absolute/path/to/halaxy-mcp/halaxy_mcp.py"],
      "tools": ["*"]
    }
  }
}
```

No `env` block is needed in any of these - the script loads its own `.env` file from next to `halaxy_mcp.py`.

## Docker / HTTP transport

For a remote MCP client that connects from cloud infrastructure rather than a local device (Claude's "custom connector", Microsoft 365 Copilot's Copilot Studio agent), run the same script over HTTP instead of stdio: `MCP_TRANSPORT=http` starts a `uvicorn` server instead of talking over stdin/stdout - the Dockerfile sets this for you.

**Auth**: real OAuth 2.1 with Dynamic Client Registration, not a static token - leave any "OAuth Client ID/Secret" fields in your MCP client blank, since there's no pre-registered client to use instead.

This server acts as its own minimal, self-contained OAuth authorization server (adapted from Anthropic's reference pattern, [`examples/servers/simple-auth`](https://github.com/modelcontextprotocol/python-sdk/tree/main/examples/servers/simple-auth)). Registered clients and access tokens are **persisted to a small JSON file** (`MCP_OAUTH_STATE_FILE`; in Docker this points at `/data`, mounted as a volume) so a redeploy doesn't force every connected client to reconnect. That file holds live access tokens, so it's `0600`-permissioned, written atomically, gitignored, and never baked into the image. "Signing in" is one shared username/password (`MCP_LOGIN_USERNAME`/`MCP_LOGIN_PASSWORD`) - fine for a small practice, not a per-person identity system. Access tokens last 1 hour; clients renew silently using a refresh token (rotated on every use) instead of resurfacing the login page each time - only once every 90 days does a human need to sign in again. `MCP_PUBLIC_URL` is the public HTTPS URL clients reach this server at (behind Caddy) - the OAuth issuer/redirect base, not the same as the internal `MCP_HOST`/`MCP_PORT`.

### Security hardening (credit: Igal Belkin, GrowInsight)

Igal Belkin (GrowInsight) independently security-reviewed this server's OAuth implementation and reported several real issues in it, since fixed. As part of that:

- New client registrations are restricted to `MCP_ALLOWED_REDIRECT_URI_HOSTS` (comma-separated hostnames) when set, and the login page shows the requesting client's name and redirect target before you sign in. **Set this before treating a deployment as done** - add only the hosts for the AI services you actually use:

  | AI service | Host to add | Notes |
  |---|---|---|
  | Claude (custom connector) | `claude.ai` | |
  | ChatGPT (connector) | `chatgpt.com` | |
  | Microsoft 365 Copilot (via Copilot Studio) | `global.consent.azure-apim.net` | Registers under the client name "Credential Manager" - Microsoft's own Power Platform OAuth consent host |
  | OpenAI Codex CLI, or any other native/CLI MCP client | `127.0.0.1` | Loopback redirect with a random port each session ([RFC 8252](https://datatracker.ietf.org/doc/html/rfc8252) native-app pattern) - matched by hostname only, so one entry covers every port |

  See `.env.example` for how to find a host not listed above, and how to recover a client that cached a `client_id` from before this setting existed.
- The login page HTML-escapes everything it renders, rejects an unrecognised `state` outright, and sends `Content-Security-Policy`/`X-Frame-Options`/`X-Content-Type-Options` headers.
- `POST /login/callback` locks out an IP for 15 minutes after 5 failed attempts.
- `requirements.txt` floors on `mcp>=1.27.2`.
- `description` is never returned for a meeting, not just a session - a real meeting titled with a client's name showed this was still possible even with no linked Patient. Only its `availability_hint` verdict comes back, never the underlying text.
- Meetings starting with "Case Conference" or "CC with" are now categorised as `meeting_category: "case_conference"` - lets a caller know that type of meeting exists without ever seeing who it's about.
- Real refresh tokens are now issued (rotated on every use) - previously every client had to redo the full interactive login every time its 1-hour access token expired.

**Test locally** (no TLS - fine for local testing, not for internet exposure):

```bash
cp .env.example .env   # fill in your Halaxy credentials + MCP_LOGIN_USERNAME/PASSWORD
docker compose up --build
curl http://127.0.0.1:8000/health   # -> 200, no auth needed
curl http://127.0.0.1:8000/mcp      # -> 401, no token
curl http://127.0.0.1:8000/.well-known/oauth-authorization-server  # -> OAuth discovery metadata
```

An MCP client does the rest automatically (register → authorize → login → token exchange).

**Deploy it somewhere internet-facing** (e.g. a Raspberry Pi behind your own router): use `docker-compose.pi.yml` instead, which adds [Caddy](https://caddyserver.com/) in front for TLS (Let's Encrypt, auto-issued/renewed) and doesn't publish the app's port directly - only Caddy is reachable from outside the container network.

This builds **directly on the target device** - no cross-compilation needed. Works on both 64-bit (`arm64`/`aarch64` - Pi 3B+ and up) and 32-bit (`armv7`/`armhf` - a Pi 2, or a Pi 3/4 on 32-bit Raspberry Pi OS).

```bash
# check what you're running, if unsure:
uname -m   # armv7l = 32-bit, aarch64 = 64-bit

# install Docker via its official apt repo (not the get.docker.com script -
# Docker's own docs say that's not recommended for anything you'll keep
# running). Covers armhf as well as arm64/amd64:
sudo apt update
sudo apt install -y ca-certificates curl
sudo install -m 0755 -d /etc/apt/keyrings
sudo curl -fsSL https://download.docker.com/linux/debian/gpg -o /etc/apt/keyrings/docker.asc
sudo chmod a+r /etc/apt/keyrings/docker.asc
echo \
  "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/debian \
  $(. /etc/os-release && echo "$VERSION_CODENAME") stable" | \
  sudo tee /etc/apt/sources.list.d/docker.list > /dev/null
sudo apt update
sudo apt install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
sudo usermod -aG docker $USER   # log out/in afterwards

git clone https://github.com/ryanhunt/halaxy-mcp.git
cd halaxy-mcp
cp .env.example .env && nano .env   # Halaxy credentials, MCP_LOGIN_USERNAME/PASSWORD, MCP_PUBLIC_URL
cp Caddyfile.example Caddyfile && nano Caddyfile   # your real domain/DDNS hostname

docker compose -f docker-compose.pi.yml up -d --build
```

`MCP_PUBLIC_URL` (in `.env`) must be the real `https://your-domain` clients will connect to - it's used as the OAuth issuer/redirect base and has to match exactly. `docker compose -f docker-compose.pi.yml ps`/`logs -f` to check it's up; `restart: unless-stopped` means it survives a reboot as long as Docker itself starts on boot. To update later: `git pull && docker compose -f docker-compose.pi.yml up -d --build` - or `./update.sh`, which does the same and polls `/health` afterwards. Nothing in `.env`/`Caddyfile` is touched by `git pull` (both are gitignored).

**32-bit ARM (`armv7`) builds need a C compiler**, already handled in the Dockerfile: `cryptography` compiles a small piece (`cffi`) from source on `armv7` (no prebuilt wheel), needing `gcc`/`libc6-dev`/`libffi-dev` - installed before `pip install` and removed afterwards to keep the image small.

You'll still need to handle port-forwarding (80+443) and firewall rules on your own network/router - and as defense-in-depth on top of the login gate, consider restricting inbound traffic to your MCP client's currently-published outbound IP ranges.

## Confirmed working with

- **Claude** (custom connector) - full OAuth flow verified against a real deployment.
- **GitHub Copilot** (VS Code, Visual Studio, Copilot CLI) - the stdio path is verified; the HTTP/OAuth path should work the same way (same standard OAuth 2.1 + DCR flow) but hasn't specifically been tried.
- **Microsoft 365 Copilot** (via a Copilot Studio agent with an MCP tool) - see [Setting this up in Microsoft 365 Copilot](#setting-this-up-in-microsoft-365-copilot) below.
- **ChatGPT** - works unmodified.

### Setting this up in Microsoft 365 Copilot

Microsoft 365 Copilot doesn't let you add a private server directly to its connector gallery (that requires Microsoft's review/approval). The self-service path is **Copilot Studio** - a separate product used to build a small "agent" published into Teams/Microsoft 365 Copilot:

1. In Copilot Studio, create an **Agent** (not "Workflow").
2. On the agent's **Tools** page: **Add a tool** → **New tool** → **Model Context Protocol**.
3. **Server URL**: `https://your-domain.example.com/mcp`. **Authentication type**: OAuth 2.0.
4. Try **"Dynamic discovery"** first. If it fails, fall back to **"Dynamic"** and fill in manually:
   - **Authorization URL**: `https://your-domain.example.com/authorize`
   - **Token URL template**: `https://your-domain.example.com/token`
5. Write **Instructions** for the agent describing what it can help with and its real limits.
6. **Publish**, selecting **Teams + Microsoft 365** as the channel.

**Licensing**: building an agent needs Copilot Studio access (a trial license can build/preview but not publish). *Using* a published agent through Copilot Chat/Teams needs a real (paid, not free "Basic") Microsoft 365 Copilot license per user - agent actions are "No charge" against that license per Microsoft's billing docs. If a newly-assigned license doesn't seem to take effect, try a full sign-out/sign-in - propagation can lag.

## Known limitations, worth knowing about

- **`list_invoices`'s lookback window can miss invoices.** Halaxy's `Invoice` search has no parameter for the invoice's own `date` field, only `created`/`_lastUpdated` - so `list_invoices` fetches invoices created in the last 45 days and filters client-side for an exact `date` match. Insurer/employer-billed invoices can be created months before the session they end up dated for, falling outside that window. `list_appointments` and `list_invoices_by_payer` don't have this problem - prefer those when it matters.
- **`session` vs. `meeting` is inferred from whether the appointment has a linked `Patient` participant**, not any explicit Halaxy field - a session booked without linking a patient record would be miscategorised as a meeting.
- No write operations (create/update anything) are implemented, on purpose.
- The HTTP transport's OAuth authorization server has one shared login (not per-person accounts) - see [Docker / HTTP transport](#docker--http-transport) above.

## What it deliberately doesn't do

This wraps a handful of read-only endpoints matching one practice's own needs, not a general Halaxy/FHIR client. It does not implement patient creation/updates, clinical notes, scheduling changes, or most of Halaxy's ~50-resource FHIR surface (referral *tracking* is covered, not creating/updating referrals). If you need more of the API, the tool functions in `halaxy_mcp.py` are a reasonably short, readable starting point to extend from.

## License

GPLv3 - see [LICENSE](LICENSE). See also [DISCLAIMER.md](DISCLAIMER.md) - no warranty, do your own research before deploying this with real patient data.

[![M8ven Score](https://m8ven.ai/badge/mcp/ryanhunt-halaxy-mcp-ose55a?v=ca92d2d8b2f0350d41bcf7286ef0ff49)](https://m8ven.ai/mcp/ryanhunt-halaxy-mcp-ose55a)
