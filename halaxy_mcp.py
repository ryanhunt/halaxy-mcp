#!/usr/bin/env python3
"""
Halaxy MCP server.

Tools: list_invoices(date), list_appointments(date), list_practitioners(),
list_invoices_by_payer(payer_name). Talks to Halaxy using the OAuth
client_credentials flow. Matches the permissions enabled on the
"ClaudeMCPLimited" Halaxy API key (Appointments -> Retrieve, Invoices &
Payments -> Retrieve / Retrieve Fees, Practitioners -> Retrieve,
Patients -> Retrieve, Claims & Referrals -> Retrieve Claim).

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

Patient data is deliberately minimised: Halaxy's Patient resource also
carries DOB, address, gender, emergency contact, and referral-source
notes, but none of that is needed here. Every Patient lookup is filtered
through ALLOWED_PATIENT_FIELDS (id, name, initials, telecom,
patient_status, is_active_client) in `_get_patient` - that filtering is
enforced in code, not just by convention, so those fields can never reach
Claude through this server regardless of what's asked for. There is no
tool, and no plan to add one, that accepts a "which fields" parameter for
Patient data - if asked for a patient's DOB/address/gender, the correct
answer is that this server cannot provide it, not an attempt to fetch it
some other way.

Transport: stdio only, for local testing via Claude Desktop's
claude_desktop_config.json. The HTTP/remote version (for the
Raspberry Pi + Caddy + Cowork setup) is a follow-up step once this
is confirmed working end to end.
"""

import json
import os
import re
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP

# Load .env from next to this script, not from whatever directory the
# process happens to be launched from (Claude Desktop won't necessarily
# set the working directory to this folder).
load_dotenv(Path(__file__).resolve().parent / ".env")

HALAXY_API_BASE = os.environ.get("HALAXY_API_BASE", "https://au-api.halaxy.com/main").rstrip("/")
HALAXY_CLIENT_ID = os.environ.get("HALAXY_CLIENT_ID")
HALAXY_CLIENT_SECRET = os.environ.get("HALAXY_CLIENT_SECRET")

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


mcp = FastMCP("halaxy-mcp")

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
        name/patient details, status, and amounts), for Claude to read
        and summarise. `patient` is only populated when the invoice's
        recipient is an actual Patient (not an insurer/employer, e.g.
        workers' comp invoices billed to a company) - `payer_name` (from
        the invoice's own title) always covers that case either way.
    """
    if date is None:
        date = datetime.now(PRACTICE_TIMEZONE).strftime("%Y-%m-%d")

    since = (datetime.strptime(date, "%Y-%m-%d") - timedelta(days=INVOICE_LOOKBACK_DAYS)).strftime("%Y-%m-%d")
    candidates = _fetch_invoices_created_since(since)
    matches = [inv for inv in candidates if inv.get("date") == date]

    summary = [
        {
            "id": inv.get("id"),
            "payer_name": inv.get("title"),
            "patient": (
                _get_patient(_ref_id(inv["recipient"]["reference"]))
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


def _initials(name_list: list[dict]) -> str | None:
    """First-letter-of-given + first-letter-of-family initials, e.g. 'Ms Jane Citizen' -> 'JC'."""
    name = _preferred_name(name_list)
    if not name:
        return None
    given = name.get("given", [])
    family = name.get("family", "")
    letters = (given[0][0] if given else "") + (family[0] if family else "")
    return letters.upper() or None


PATIENT_STATUS_EXTENSION_URL = "https://terminology.halaxy.com/StructureDefinition/patient-status"

# Halaxy's own docs (support.halaxy.com, "Set a patient status") define these:
#   current   - actively active in the practice, selectable for appointments
#   contact   - not yet a patient; a lead/contact record, not an active client
#   archived  - inactive, but can still be selected for appointments
#   blocked   - can no longer be selected for appointments
#   deceased  - can no longer be selected for appointments
# "current" is the only status that means an actual active client - the
# plain FHIR `active` boolean is too coarse for this (it's also true for
# "contact", which isn't a client at all yet).
ACTIVE_CLIENT_STATUS = "current"

# Hard allowlist of every field this server is permitted to return about a
# patient - deliberately narrow. Halaxy's Patient resource also carries
# DOB, address, gender, emergency contact, and referral-source notes; none
# of that is needed here, and _patient_summary must never be extended to
# include it. This set is enforced (not just documented) by
# `_get_patient`, so accidentally adding a field to _patient_summary later
# still gets stripped before it ever reaches Claude.
ALLOWED_PATIENT_FIELDS = {"id", "name", "initials", "telecom", "patient_status", "is_active_client"}


def _patient_summary(patient: dict) -> dict:
    """Reduce a Patient resource to just the fields this server needs - see ALLOWED_PATIENT_FIELDS.

    Deliberately drops everything else Halaxy returns on a Patient (DOB,
    address, gender, emergency contact, referral-source notes, etc.) -
    none of it is needed here, so it's discarded at this boundary rather
    than passed through to Claude.
    """
    status = next(
        (
            ext.get("valueString")
            for ext in patient.get("extension", [])
            if ext.get("url") == PATIENT_STATUS_EXTENSION_URL
        ),
        None,
    )
    return {
        "id": patient.get("id"),
        "name": _human_name(patient.get("name", [])),
        "initials": _initials(patient.get("name", [])),
        "telecom": [
            {"system": t.get("system"), "value": t.get("value"), "use": t.get("use")}
            for t in patient.get("telecom", [])
        ],
        "patient_status": status,
        "is_active_client": status == ACTIVE_CLIENT_STATUS,
    }


# Patient demographics change rarely enough, and are looked up by the same
# handful of IDs across appointments/invoices in one call, that an
# in-memory cache avoids re-fetching the same patient repeatedly.
PATIENT_CACHE_TTL_SECONDS = 6 * 60 * 60
_patient_cache: dict[str, dict] = {}


def _get_patient(patient_id: str) -> dict | None:
    """Fetch (or reuse a cached) reduced patient summary - see `_patient_summary`.

    Every return path goes through the ALLOWED_PATIENT_FIELDS allowlist
    here, not just through `_patient_summary` - so this is the one place
    that has to be trusted to never leak DOB/address/gender/etc, even if
    `_patient_summary` is edited incorrectly later.
    """
    cached = _patient_cache.get(patient_id)
    if cached is not None and time.time() < cached["expires_at"]:
        return cached["summary"]

    patient = _halaxy_get(f"Patient/{patient_id}", {})
    if patient.get("resourceType") != "Patient":
        return None

    summary = {k: v for k, v in _patient_summary(patient).items() if k in ALLOWED_PATIENT_FIELDS}
    assert set(summary) <= ALLOWED_PATIENT_FIELDS
    _patient_cache[patient_id] = {"summary": summary, "expires_at": time.time() + PATIENT_CACHE_TTL_SECONDS}
    return summary


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

    Practitioners and patients are both resolved via this API key's
    "Practitioners" and "Patients" permissions. Patient data is
    deliberately minimal - only name/initials/telecom, never DOB, address,
    or anything else Halaxy holds on the patient.

    Args:
        date: Date in YYYY-MM-DD format. Defaults to today (Australia/Sydney).
        appointment_type: Optionally filter to just "session" or "meeting".
            Omit to return both.

    Sessions also carry a `session_mode` ("F2F" or "Telehealth" in this
    account) - Halaxy models this via the HealthcareService the
    appointment is booked against (Appointment.supportingInformation),
    not the description field. Meetings have no HealthcareService, so
    this is always null for them.

    A session with no invoice yet also carries `awaiting_insurer_invoice`
    - populated (with the insurer/employer name) only when the patient
    has an active Coverage on file flagged "billed to organisation"
    (Halaxy's own mechanism for routing an invoice to an insurer/employer
    instead of the patient) - i.e. a session that's expected to be billed
    to a specific insurer, but hasn't been yet. Use `list_invoices_by_payer`
    to check that insurer's actual invoice history.

    Returns:
        JSON with the target date and each appointment's type, time,
        description, session mode, patient (id/name/initials/telecom, or
        null for a meeting), practitioner name (and role ID), linked
        invoice details (or null), and awaiting_insurer_invoice.
    """
    if date is None:
        date = datetime.now(PRACTICE_TIMEZONE).strftime("%Y-%m-%d")
    if appointment_type not in (None, "session", "meeting"):
        raise ValueError('appointment_type must be "session", "meeting", or omitted')

    data = _halaxy_get("Appointment", {"date": f"eq{date}", "_count": 100})
    appointments = [
        entry["resource"]
        for entry in data.get("entry", [])
        if entry.get("resource", {}).get("resourceType") == "Appointment"
    ]

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
                "description": appt.get("description"),
                "session_mode": (
                    _get_healthcare_service_name(service_id)
                    if (service_id := _healthcare_service_id(appt))
                    else None
                ),
                "patient": _get_patient(refs["patient_id"]) if refs["patient_id"] else None,
                "practitioner_role_id": refs["practitioner_role_id"],
                "practitioner_name": practitioner_role_names.get(refs["practitioner_role_id"], {}).get("name"),
                "invoice_id": refs["invoice_id"],
            }
        )
        if refs["invoice_id"]:
            invoice_ids_to_resolve.add(refs["invoice_id"])

    invoices_by_id = {}
    for invoice_id in invoice_ids_to_resolve:
        invoice = _halaxy_get(f"Invoice/{invoice_id}", {})
        if invoice.get("resourceType") == "Invoice":
            invoices_by_id[invoice_id] = {
                "payer_name": invoice.get("title"),
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
            _get_patient_insurer_coverage(item["patient"]["id"])
            if invoice is None and item["appointment_type"] == "session" and item["patient"]
            else None
        )

    return json.dumps(
        {"queried_date": date, "appointment_count": len(parsed), "appointments": parsed},
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


if __name__ == "__main__":
    mcp.run()
