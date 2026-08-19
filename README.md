# halaxy-mcp

An [MCP](https://modelcontextprotocol.io) server for the [Halaxy](https://www.halaxy.com/) practice-management API, written in Python. It lets an MCP client (Claude, GitHub Copilot, etc.) answer questions like "what's on my calendar today", "which of today's appointments haven't been invoiced yet", or "what invoices are outstanding with a given insurer", by talking to your own Halaxy account.

This is a small, single-tenant tool built for one practice's own use, not a general-purpose Halaxy SDK - see [What it deliberately doesn't do](#what-it-deliberately-doesnt-do) below.

## Tools

- **`list_invoices(date)`** - invoices dated a given day (defaults to today). Each invoice has a `payer_name` (always present) and a `patient` object (only present when the payer is an actual patient, not an insurer/employer).
- **`list_appointments(date, appointment_type)`** - appointments for a given day, each tagged `"session"` (a real client appointment) or `"meeting"` (a blocker/reminder/internal note - anything with no linked patient). Sessions also carry:
  - `session_mode` - `"F2F"` or `"Telehealth"`, resolved from the HealthcareService the appointment is booked against
  - `patient` - `id`/`name`/`initials`/`telecom`/`patient_status`/`is_active_client` (see [Patient data](#patient-data) below)
  - `invoice` - the linked invoice, if one's been raised, via Halaxy's direct appointment→invoice reference (more reliable than matching by date - see the notes in the code)
  - `awaiting_insurer_invoice` - populated only when there's no invoice yet *and* the patient has an active Coverage on file flagged "billed to an organisation" - i.e. flags a session that's expected to be billed to an insurer/employer but hasn't been yet
- **`list_practitioners()`** - clinical staff, each with their PractitionerRole ID and name, so a client can resolve "what's on for Alice today" to a role ID before matching it against `list_appointments`.
- **`list_invoices_by_payer(payer_name)`** - every invoice ever billed to a specific insurer/employer/organisation (e.g. "Acme Insurance"), not tied to any date - searches Halaxy's `Invoice?recipient=` directly, so it doesn't have `list_invoices`'s lookback-window blind spot (see below).

## Required Halaxy API key scopes

Create an API key in Halaxy (Settings → API Keys) with whichever of these you need - the server degrades gracefully if a scope is off, it'll just fail on the tools that need it:

| Scope (as labelled in Halaxy's UI) | Used by |
|---|---|
| Appointments → Retrieve | `list_appointments` |
| Invoices & Payments → Retrieve, Retrieve Fees | `list_invoices`, `list_invoices_by_payer` |
| Practitioners → Retrieve | `list_practitioners`, practitioner names in `list_appointments` |
| Patients → Retrieve | Patient names/telecom/status in `list_appointments` |
| Claims & Referrals → Retrieve Claim | `awaiting_insurer_invoice`, `list_invoices_by_payer` (this is Halaxy's plain-English label for read access to the FHIR `Coverage` resource) |

## Patient data

This server deliberately minimises what it exposes about a patient. Halaxy's `Patient` resource also carries DOB, address, gender, emergency contact, and referral-source notes - none of that is needed here, and it's enforced in code (`ALLOWED_PATIENT_FIELDS` in `halaxy_mcp.py`), not just by convention: every patient lookup is filtered down to `id`/`name`/`initials`/`telecom`/`patient_status`/`is_active_client` before it can reach the MCP client, regardless of what's asked for.

**Clinical/session notes are not retrievable through this API at all, for any key or scope.** Halaxy's own `/metadata` capability statement shows its clinical-notes resource (`DocumentReference`) supports `create`/`patch` only - no read, matching what the Halaxy UI itself shows (Clinical Notes only has a Create toggle). This is a whole-API limitation, not something this server chooses not to expose.

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

## Known limitations, worth knowing about

- **`list_invoices`'s lookback window can miss invoices.** Halaxy's `Invoice` search has no parameter for the invoice's own `date` field, only `created`/`_lastUpdated` - so `list_invoices` fetches invoices created in the last 45 days and filters client-side for an exact `date` match. Insurer/employer-billed invoices (e.g. workers' comp) are sometimes created months before the session they end up dated for, which can fall outside that window. `list_appointments` doesn't have this problem (it follows the appointment→invoice link directly), and `list_invoices_by_payer` doesn't either (it searches by recipient, unbounded by date) - prefer those when the date-based blind spot matters.
- **`session` vs. `meeting` is inferred from whether the appointment has a linked `Patient` participant**, not from any explicit Halaxy field - a real session booked without linking a patient record in Halaxy would be miscategorised as a meeting.
- No write operations (create/update anything) are implemented, on purpose.
- stdio transport only - a remote/HTTP variant (for hosting this somewhere reachable by a cloud-based MCP client, e.g. a custom connector) isn't built yet.

## What it deliberately doesn't do

This wraps a handful of read-only endpoints matching one practice's own needs, not a general Halaxy/FHIR client. It does not implement patient creation/updates, clinical notes, referrals, scheduling changes, or most of Halaxy's ~50-resource FHIR surface. If you need more of the API, the tool functions in `halaxy_mcp.py` are a reasonably short, readable starting point to extend from.

## License

GPLv3 - see [LICENSE](LICENSE).
