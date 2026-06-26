"""
hpecom.login
~~~~~~~~~~~~
Okta Verify push login for HPE GreenLake Compute Ops Management.

Implements the full Okta IDX auth chain:
  PingFederate (sso.common.cloud.hpe.com)
    → Okta GreenLake tenant (auth.hpe.com)  – IDX identify
    → HPE Workforce Okta (mylogin.hpe.com)  – for HPE employees
    → Okta Verify push challenge (number on phone)
    → SAMLResponse → PingFederate callback → auth code
    → Token exchange
"""

import asyncio
import base64
import hashlib
import json
import os
import re
import secrets
import sys
import time
import urllib.parse
from html import unescape
from pathlib import Path
from typing import Optional, Tuple

import httpx
import yaml
from rich.console import Console
from rich.panel import Panel
from rich.prompt import Prompt
from rich import box

from proliant.com.auth import REGION_MAP, CredentialsError

console = Console()

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
CREDS_FILE        = Path.home() / ".config" / "proliant" / "com" / "credentials.yml"
TOKEN_CACHE       = Path.home() / ".config" / "proliant" / "com" / "token.json"
GL_COMMON_URL     = "https://common.cloud.hpe.com"
SSO_URL           = "https://sso.common.cloud.hpe.com"
USER_API_BASE     = "https://aquila-user-api.common.cloud.hpe.com"
REDIRECT_URI      = f"{GL_COMMON_URL}/authentication/callback"
CLIENT_ID         = "aquila-user-auth"
POLL_INTERVAL     = 2   # seconds
POLL_TIMEOUT      = 300 # seconds (5 min)

IDX_HEADERS = {
    "Content-Type": "application/json; okta-version=1.0.0",
    "Accept":       "application/ion+json; okta-version=1.0.0",
}


# ---------------------------------------------------------------------------
# PKCE helpers
# ---------------------------------------------------------------------------

def _pkce() -> Tuple[str, str, str]:
    """Return (verifier, challenge, state)."""
    verifier  = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()
    ).rstrip(b"=").decode()
    state = secrets.token_urlsafe(16)
    return verifier, challenge, state


def _decode_js_str(raw: str) -> str:
    """Decode JavaScript hex escape sequences (\\xNN, \\uNNNN)."""
    result, i = [], 0
    while i < len(raw):
        if raw[i:i+2] == r'\x' and i + 4 <= len(raw):
            result.append(chr(int(raw[i+2:i+4], 16)))
            i += 4
        elif raw[i:i+2] == r'\u' and i + 6 <= len(raw):
            result.append(chr(int(raw[i+2:i+6], 16)))
            i += 6
        else:
            result.append(raw[i])
            i += 1
    return "".join(result)


# ---------------------------------------------------------------------------
# Auth flow steps
# ---------------------------------------------------------------------------

async def _fetch_settings() -> dict:
    """Fetch HPE GreenLake runtime settings (authority/okta/org-api URLs)."""
    try:
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.get(f"{GL_COMMON_URL}/settings.json")
            if r.status_code == 200:
                return r.json()
    except Exception:
        pass
    return {}


def _find_state_token(html: str) -> Optional[str]:
    """Scrape an Okta stateToken from an HTML page (multiple known patterns)."""
    for pat in (
        r"var\s+stateToken\s*=\s*'([^']+)'",
        r'var\s+stateToken\s*=\s*"([^"]+)"',
        r'"stateToken"\s*:\s*"([^"]+)"',
    ):
        m = re.search(pat, html)
        if m:
            return _decode_js_str(m.group(1))
    return None


async def _get_state_token(
    client: httpx.AsyncClient, challenge: str, state: str, email: str = ""
) -> Tuple[str, str]:
    """
    Start the PKCE authorize flow and extract the Okta stateToken.
    Returns (state_token, okta_base_url).

    ⚠️  UNDOCUMENTED INTERNAL ENDPOINTS — not a published HPE API contract.
    These replicate what the GreenLake web UI does. HPE may change them without
    notice. The official developer portal only documents the API-client-secret flow.

    HPE GreenLake fronts Okta with the "Pavo" SSO broker. The
    ``/as/authorization.oauth2`` request redirects to a React SPA
    (``common.cloud.hpe.com/sso/continue?track-id=…``) whose JavaScript
    normally resolves the user's IdP and bounces to the Okta sign-in page.
    Since we cannot run that JavaScript, we replicate it: when we land on
    ``/sso/continue`` we call the broker's ``sso-resolve`` endpoint with the
    user's ``login_hint`` (email) and ``track-id``. That 302s to the real
    Okta ``/authorize`` page whose HTML embeds the stateToken.

    If this breaks after an HPE update, check: settings.json for changed URLs,
    the sso-resolve path version (v1alpha2), and client ID ``aquila-user-auth``.
    """
    settings   = await _fetch_settings()
    authority  = settings.get("authorityURL", SSO_URL).rstrip("/")
    org_api    = settings.get("orgApiGw", "https://aquila-org-api.common.cloud.hpe.com").rstrip("/")
    client_id  = settings.get("client_id", CLIENT_ID)
    nonce      = secrets.token_urlsafe(16)

    params = {
        "client_id":              client_id,
        "redirect_uri":           REDIRECT_URI,
        "response_type":          "code",
        "scope":                  "openid profile email",
        "code_challenge":         challenge,
        "code_challenge_method":  "S256",
        "state":                  state,
        "nonce":                  nonce,
    }
    url = authority + "/as/authorization.oauth2?" + urllib.parse.urlencode(params)
    r = await client.get(url, follow_redirects=True)

    state_token = _find_state_token(r.text)

    # Pavo SSO broker: landed on /sso/continue?track-id=… with no stateToken.
    # Replicate the SPA's sso-resolve call to reach the Okta sign-in page.
    if not state_token:
        final = str(r.url)
        m = re.search(r"[?&]track-id=([^&]+)", final)
        if m and email:
            track_id = urllib.parse.unquote(m.group(1))
            resolve_url = (
                f"{org_api}/internal-identity/v1alpha2/sso-resolve"
                f"?login_hint={urllib.parse.quote(email)}"
                f"&track-id={urllib.parse.quote(track_id)}"
            )
            r = await client.get(resolve_url, follow_redirects=True)
            state_token = _find_state_token(r.text)

    if not state_token:
        raise AuthFlowError(
            "Could not extract stateToken from authorization response.\n"
            f"Final URL: {r.url}"
        )

    okta_base = str(r.url).split("/oauth2/")[0]  # e.g. https://auth.hpe.com
    return state_token, okta_base


async def _idx_post(client: httpx.AsyncClient, url: str, payload: dict) -> dict:
    """POST to Okta IDX endpoint; return parsed JSON."""
    r = await client.post(url, json=payload, headers=IDX_HEADERS)
    return r.json()


async def _idx_introspect(
    client: httpx.AsyncClient, okta_base: str, state_token: str
) -> Optional[dict]:
    """IDX introspect – return data dict or None if session expired."""
    data = await _idx_post(
        client,
        f"{okta_base}/idp/idx/introspect",
        {"stateToken": state_token},
    )
    if "stateHandle" not in data:
        return None  # session expired
    return data


async def _follow_saml_to_workforce(
    client: httpx.AsyncClient,
    redirect_idp: dict,
) -> dict:
    """
    Follow the redirect-idp link → submit SAML form to mylogin.hpe.com
    → follow the JS step-up redirect → IDX introspect at mylogin.hpe.com.
    Returns the IDX data at mylogin.hpe.com.
    """
    # GET auth.hpe.com SAML form page
    r4 = await client.get(redirect_idp["href"])

    form_action_raw = re.search(r'<form[^>]*action="([^"]+)"', r4.text, re.I)
    if not form_action_raw:
        # External accounts can land on an Okta Sign-In Widget page instead of
        # a server-rendered SAML form. The widget bootstraps from stateToken.
        m = re.search(r'"stateToken"\s*:\s*"([^"]+)"', r4.text)
        if not m:
            raise AuthFlowError("No SAML form or Okta stateToken found in redirect-idp response")
        okta_base = str(r4.url).split("/sso/")[0]
        data = await _idx_introspect(client, okta_base, _decode_js_str(m.group(1)))
        if data is None:
            raise AuthFlowError("Session expired at auth.hpe.com during widget introspect")
        return data, okta_base

    form_action = unescape(form_action_raw.group(1))
    inputs      = re.findall(r'<input[^>]+name="([^"]*)"[^>]*value="([^"]*)"', r4.text, re.I)
    form_data   = {n: unescape(v) for n, v in inputs}

    # POST SAML request to mylogin.hpe.com
    r5 = await client.post(form_action, data=form_data)

    # The response is JS: window.location.href = '...step-up/redirect?stateToken=...'
    js_match = re.search(r"window\.location\.href\s*=\s*'([^']+)'", r5.text)
    if not js_match:
        raise AuthFlowError(
            "No JS step-up redirect found in mylogin SAML response.\n"
            f"URL: {r5.url}"
        )
    step_up_url = _decode_js_str(js_match.group(1))

    # Extract stateToken from the step-up URL
    parsed   = urllib.parse.urlparse(step_up_url)
    qs       = urllib.parse.parse_qs(parsed.query)
    state_t2 = qs.get("stateToken", [None])[0]
    if not state_t2:
        raise AuthFlowError(f"No stateToken in step-up URL: {step_up_url}")

    okta_base2 = f"{parsed.scheme}://{parsed.netloc}"  # https://mylogin.hpe.com

    # IDX introspect at mylogin.hpe.com
    data = await _idx_introspect(client, okta_base2, state_t2)
    if data is None:
        raise AuthFlowError("Session expired at mylogin.hpe.com during introspect")
    return data, okta_base2


async def _select_okta_verify_push(
    client: httpx.AsyncClient,
    okta_base: str,
    idx_data: dict,
) -> Tuple[str, str, Optional[int]]:
    """
    Select Okta Verify (push) authenticator.
    Returns (poll_href, state_handle, correct_answer_or_None).
    """
    state_handle = idx_data["stateHandle"]
    select_href  = next(
        v["href"]
        for v in idx_data["remediation"]["value"]
        if v["name"] == "select-authenticator-authenticate"
    )

    # Find Okta Verify authenticator
    authenticators = idx_data.get("authenticators", {}).get("value", [])
    okta_verify = next(
        (a for a in authenticators
         if "okta_verify" in a.get("key", "").lower()
         or "okta verify" in a.get("displayName", "").lower()),
        None,
    )
    if not okta_verify:
        available = [a.get("displayName") for a in authenticators]
        raise AuthFlowError(
            f"Okta Verify not available. Authenticators: {available}"
        )

    # Prefer push, fallback to totp
    methods       = [m["type"] for m in okta_verify.get("methods", [])]
    method_type   = "push" if "push" in methods else ("totp" if "totp" in methods else methods[0])

    # Select authenticator
    r = await client.post(
        select_href,
        json={
            "authenticator": {"id": okta_verify["id"], "methodType": method_type},
            "stateHandle":   state_handle,
        },
        headers=IDX_HEADERS,
    )
    d = r.json()

    if method_type == "push":
        # Get poll href and correctAnswer
        poll_href = next(
            (v["href"] for v in d.get("remediation", {}).get("value", [])
             if v["name"] == "challenge-poll"),
            None,
        )
        if not poll_href:
            raise AuthFlowError("No challenge-poll href in push challenge response")

        current_auth  = d.get("currentAuthenticator", {}).get("value", {})
        correct_answer = current_auth.get("contextualData", {}).get("correctAnswer")

        return poll_href, d["stateHandle"], correct_answer

    else:  # totp
        return None, d["stateHandle"], None, d


async def _poll_push(
    client:       httpx.AsyncClient,
    poll_href:    str,
    state_handle: str,
    timeout:      int = POLL_TIMEOUT,
) -> str:
    """
    Poll the push challenge until approved.
    Returns the success redirect URL.
    """
    deadline = time.monotonic() + timeout
    payload  = {"stateHandle": state_handle}

    while True:
        r = await client.post(poll_href, json=payload, headers=IDX_HEADERS)
        d = r.json()

        # Check for push denial / error
        msgs = d.get("messages", {}).get("value", [])
        if any(m.get("class") == "ERROR" for m in msgs):
            err = msgs[0].get("message", "Unknown error") if msgs else "Push denied"
            raise AuthFlowError(f"Okta Verify push denied: {err}")

        # Check for success
        success_href = (
            (d.get("success") or {}).get("href")
            or (d.get("successWithInteractionCode") or {}).get("href")
        )
        if success_href:
            return success_href

        # Check for MFA step-up (Push + Password not supported)
        if any(v["name"] == "challenge-authenticator" for v in d.get("remediation", {}).get("value", [])):
            raise AuthFlowError(
                "Push + additional factor (MFA step-up) is not supported.\n"
                "Please configure Okta Verify push alone."
            )

        if time.monotonic() >= deadline:
            raise AuthFlowError(
                f"Okta Verify push timed out after {timeout}s. "
                "Please try again."
            )

        await asyncio.sleep(POLL_INTERVAL)


async def _password_authenticate(
    client: httpx.AsyncClient,
    okta_base: str,
    idx_data: dict,
    password: str,
) -> str:
    """
    Authenticate using password for external (non-corporate SSO) accounts.

    Flow:
      1. Select password authenticator from select-authenticator-authenticate
      2. POST password to challenge/answer
      3. Return success href for token exchange

    Raises AuthFlowError on wrong password or unexpected MFA challenge.
    """
    state_handle = idx_data["stateHandle"]
    remediations = idx_data.get("remediation", {}).get("value", [])
    authenticators = idx_data.get("authenticators", {}).get("value", [])

    # ── Step 1: Select password authenticator ────────────────────────────
    select_v = next(
        (v for v in remediations if v["name"] == "select-authenticator-authenticate"),
        None,
    )
    if not select_v:
        raise AuthFlowError(
            f"No select-authenticator-authenticate available. "
            f"Remediations: {[v['name'] for v in remediations]}"
        )

    password_auth = next(
        (a for a in authenticators if a.get("key") == "okta_password"
         or "password" in a.get("displayName", "").lower()),
        None,
    )
    if not password_auth:
        available = [a.get("displayName") for a in authenticators]
        raise AuthFlowError(
            f"Password authenticator not available. Available: {available}"
        )

    r = await client.post(
        select_v["href"],
        json={
            "authenticator": {"id": password_auth["id"]},
            "stateHandle":   state_handle,
        },
        headers=IDX_HEADERS,
    )
    d = r.json()

    # ── Step 2: Submit password ───────────────────────────────────────────
    challenge_v = next(
        (v for v in d.get("remediation", {}).get("value", [])
         if v["name"] == "challenge-authenticator"),
        None,
    )
    if not challenge_v:
        raise AuthFlowError(
            f"No challenge-authenticator after selecting password. Got: "
            f"{[v['name'] for v in d.get('remediation', {}).get('value', [])]}"
        )

    r2 = await client.post(
        challenge_v["href"],
        json={
            "credentials": {"passcode": password},
            "stateHandle": d["stateHandle"],
        },
        headers=IDX_HEADERS,
    )
    d2 = r2.json()

    # Check for error messages (wrong password)
    msgs = d2.get("messages", {}).get("value", [])
    if any(m.get("class") == "ERROR" for m in msgs):
        err = msgs[0].get("message", "Authentication failed") if msgs else "Wrong password"
        raise AuthFlowError(f"Password authentication failed: {err}")

    # Check for success
    success_href = (
        (d2.get("success") or {}).get("href")
        or (d2.get("successWithInteractionCode") or {}).get("href")
    )
    if success_href:
        return success_href

    # MFA required after password — check for Okta Verify push
    remediations2 = [v["name"] for v in d2.get("remediation", {}).get("value", [])]
    if "select-authenticator-authenticate" in remediations2:
        # Return d2 so caller can chain to Okta Verify if needed
        raise _MFARequired(d2, okta_base)

    raise AuthFlowError(
        f"Unexpected response after password submit. "
        f"Remediations: {remediations2}"
    )


class _MFARequired(Exception):
    """Raised when password auth succeeds but MFA is still required."""
    def __init__(self, idx_data: dict, okta_base: str):
        self.idx_data  = idx_data
        self.okta_base = okta_base


async def _extract_code_from_redirects(
    client: httpx.AsyncClient,
    start_url: str,
) -> str:
    """
    Follow the redirect chain from IDX success href until we find
    ?code= in the URL (the OAuth2 authorization code).

    Confirmed chain (via debug):
      1. GET success_href (mylogin/login/token/redirect?stateToken=…) → 200 HTML
         with SAMLResponse form → POST to auth.hpe.com/sso/saml2/…
      2. POST response → 200 HTML with NEW stateToken for auth.hpe.com
      3. Introspect at auth.hpe.com with new stateToken → success.href
      4. GET success.href → 302 → sso.common.cloud.hpe.com/sp/… → code=…
    """
    # Step 1: GET mylogin success href → HTML with SAMLResponse form
    r = await client.get(start_url, follow_redirects=True)

    # Direct HPE Account (non-federated): the success href redirects straight
    # to the callback with ?code=… — no intermediate SAMLResponse form.
    qs0 = urllib.parse.parse_qs(urllib.parse.urlparse(str(r.url)).query)
    if qs0.get("code"):
        return qs0["code"][0]

    saml_match = re.search(r'<form[^>]+action="([^"]+)"', r.text, re.I)
    if not saml_match:
        raise AuthFlowError(
            f"Expected SAMLResponse form, got: {str(r.url)}"
        )

    action = unescape(saml_match.group(1))
    inputs = re.findall(r'<input[^>]+name="([^"]*)"[^>]*value="([^"]*)"', r.text, re.I)
    form_data = {n: unescape(v) for n, v in inputs}

    # Step 2: POST SAMLResponse → auth.hpe.com ACS → response has new stateToken
    rp = await client.post(action, data=form_data, follow_redirects=True)

    # Quick check: code in URL after SAML POST (unlikely but safe)
    qs = urllib.parse.parse_qs(urllib.parse.urlparse(str(rp.url)).query)
    if qs.get("code"):
        return qs["code"][0]

    # Step 3: Extract new stateToken from auth.hpe.com response page
    m = re.search(r'"stateToken"\s*:\s*"([^"]+)"', rp.text)
    if not m:
        raise AuthFlowError(
            f"No stateToken in auth.hpe.com SAML response. URL: {str(rp.url)}"
        )
    state_token_new = _decode_js_str(m.group(1))

    data = await _idx_introspect(client, "https://auth.hpe.com", state_token_new)
    success_href = (
        (data.get("success") or {}).get("href")
        or (data.get("successWithInteractionCode") or {}).get("href")
    )
    if not success_href:
        raise AuthFlowError("No success.href after auth.hpe.com SAML introspect")

    # Step 4: GET success.href → follows redirect → auth code in URL
    r2 = await client.get(success_href, follow_redirects=True)
    qs2 = urllib.parse.parse_qs(urllib.parse.urlparse(str(r2.url)).query)
    code = qs2.get("code", [None])[0]
    if code:
        return code

    raise AuthFlowError(
        f"Could not extract authorization code. Final URL: {str(r2.url)}"
    )


async def _exchange_token(
    verifier:  str,
    state:     str,
    auth_code: str,
) -> dict:
    """
    Exchange the PKCE authorization code for an access token.
    Returns the full token response dict.
    """
    token_url = f"{SSO_URL}/as/token.oauth2"
    async with httpx.AsyncClient(timeout=15) as c:
        r = await c.post(
            token_url,
            data={
                "grant_type":    "authorization_code",
                "code":          auth_code,
                "redirect_uri":  REDIRECT_URI,
                "code_verifier": verifier,
                "client_id":     CLIENT_ID,
            },
        )
    if r.status_code != 200:
        raise AuthFlowError(
            f"Token exchange failed ({r.status_code}): {r.text[:300]}"
        )
    return r.json()


# ---------------------------------------------------------------------------
# Workspace session (ccs-session cookie)
# ---------------------------------------------------------------------------

async def _init_workspace_session(
    access_token: str,
    id_token: str,
) -> Tuple[list, str]:
    """
    POST /authn/v1/session → returns (active_workspaces_list, initial_ccs_session).
    Does NOT load a workspace yet.
    """
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.post(
            f"{USER_API_BASE}/authn/v1/session",
            json={"id_token": id_token},
            headers={
                "Authorization": f"Bearer {access_token}",
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
        )
        if r.status_code != 200:
            raise AuthFlowError(
                f"Workspace session creation failed ({r.status_code}): {r.text[:300]}"
            )
        data = r.json()
        workspaces = [
            w for w in data.get("accounts", [])
            if w.get("account_status") == "ACTIVE"
        ]
        if not workspaces:
            raise AuthFlowError("No active workspaces found in HPE GreenLake account.")
        ccs_session = ""
        for c in client.cookies.jar:
            if c.name == "ccs-session":
                ccs_session = c.value
                break
        return workspaces, ccs_session


async def _load_workspace(
    access_token: str,
    workspace_id: str,
    ccs_session: str,
) -> str:
    """
    GET /authn/v1/session/load-account/{id} → returns updated ccs_session.
    """
    async with httpx.AsyncClient(timeout=15) as client:
        cookies = {"ccs-session": ccs_session} if ccs_session else None
        r = await client.get(
            f"{USER_API_BASE}/authn/v1/session/load-account/{workspace_id}",
            headers={"Authorization": f"Bearer {access_token}"},
            cookies=cookies,
        )
        if r.status_code not in (200, 204):
            raise AuthFlowError(
                f"Workspace load failed ({r.status_code}): {r.text[:300]}"
            )
        for c in client.cookies.jar:
            if c.name == "ccs-session":
                return c.value
        return ccs_session  # unchanged if cookie not refreshed


async def _pick_workspace(
    workspaces: list,
    workspace_name: Optional[str] = None,
) -> dict:
    """
    Select a workspace by name or interactively (arrow keys + type to search).
    Returns the chosen workspace dict.
    """
    if len(workspaces) == 1:
        return workspaces[0]

    if workspace_name:
        ws = next((w for w in workspaces if w["company_name"] == workspace_name), None)
        if not ws:
            names = [w["company_name"] for w in workspaces]
            raise AuthFlowError(
                f"Workspace '{workspace_name}' not found. Available: {names}"
            )
        return ws

    # Interactive picker — run questionary in a thread (it blocks; needs no running loop)
    names = [w["company_name"] for w in workspaces]
    try:
        import questionary

        def _ask() -> Optional[str]:
            return questionary.select(
                "Select workspace:",
                choices=names,
                use_search_filter=True,
                instruction="(↑↓ arrows · type to filter · Enter to confirm)",
            ).ask()

        chosen = await asyncio.to_thread(_ask)
    except ImportError:
        # Fallback: numbered list
        console.print("\n[bold]Available workspaces:[/bold]")
        for i, name in enumerate(names, 1):
            console.print(f"  [cyan]{i:2}.[/cyan] {name}")
        from rich.prompt import Prompt
        while True:
            answer = Prompt.ask("\nWorkspace number or partial name").strip()
            if answer.isdigit():
                idx = int(answer) - 1
                if 0 <= idx < len(workspaces):
                    chosen = names[idx]
                    break
            else:
                matches = [n for n in names if answer.lower() in n.lower()]
                if len(matches) == 1:
                    chosen = matches[0]
                    break
                elif matches:
                    console.print(f"[yellow]Ambiguous: {matches}[/yellow]")
                else:
                    console.print("[red]No match found.[/red]")
            chosen = None

    if chosen is None:  # Ctrl+C in questionary
        console.print("[yellow]No workspace selected — using first available.[/yellow]")
        return workspaces[0]

    return next(w for w in workspaces if w["company_name"] == chosen)


async def _setup_workspace_session(
    access_token: str,
    id_token: str,
    workspace_name: Optional[str] = None,
) -> Tuple[str, str, str, list]:
    """
    Full workspace session setup (non-interactive). Auto-selects first workspace
    when multiple exist. Used by refresh_token_if_needed and legacy callers.
    Returns (workspace_id, workspace_name, ccs_session, workspaces).
    """
    workspaces, ccs_session = await _init_workspace_session(access_token, id_token)

    if workspace_name:
        ws = next((w for w in workspaces if w["company_name"] == workspace_name), None)
        if not ws:
            names = [w["company_name"] for w in workspaces]
            raise AuthFlowError(
                f"Workspace '{workspace_name}' not found. Available: {names}"
            )
    else:
        ws = workspaces[0]
        if len(workspaces) > 1:
            names = [w["company_name"] for w in workspaces]
            console.print(
                f"[dim]Multiple workspaces: {names}. "
                f"Using '{ws['company_name']}'. Pass --workspace to choose.[/dim]"
            )

    workspace_id = ws["platform_customer_id"]
    ccs_session = await _load_workspace(access_token, workspace_id, ccs_session)
    return workspace_id, ws.get("company_name", ""), ccs_session, workspaces


# ---------------------------------------------------------------------------
# Token persistence
# ---------------------------------------------------------------------------

def save_token(token: dict, region: str = "us-west",
               workspace_id: str = "", workspace_name: str = "",
               ccs_session: str = "",
               workspaces: Optional[list] = None,
               glp_client_id: str = "",
               glp_client_secret: str = "",
               glp_credential_name: str = "") -> None:
    """Persist the user OAuth token to ~/.config/proliant/com/token.json."""
    TOKEN_CACHE.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "access_token":        token.get("access_token"),
        "refresh_token":       token.get("refresh_token"),
        "id_token":            token.get("id_token"),
        "expires_at":          time.time() + token.get("expires_in", 7200),
        "region":              region,
        "token_type":          "user",
        "workspace_id":        workspace_id,
        "workspace_name":      workspace_name,
        "ccs_session":         ccs_session,
        "workspaces":          workspaces or [],
        "glp_client_id":       glp_client_id,
        "glp_client_secret":   glp_client_secret,
        "glp_credential_name": glp_credential_name,
    }
    TOKEN_CACHE.write_text(json.dumps(payload, indent=2))
    TOKEN_CACHE.chmod(0o600)


def cache_glp_token(access_token: str, expires_in: int) -> None:
    """Persist a GLP client-credentials token into the existing token.json.

    This allows the next CLI invocation to reuse the token without fetching
    a new one (~0.3s savings per run when Okta session is expired).
    """
    if not TOKEN_CACHE.exists():
        return
    # Sanity check: only cache real JWTs (at least 2 dots, length > 50)
    if access_token.count(".") < 2 or len(access_token) < 50:
        return
    try:
        data = json.loads(TOKEN_CACHE.read_text())
        data["glp_access_token"] = access_token
        data["glp_token_expires_at"] = time.time() + expires_in
        TOKEN_CACHE.write_text(json.dumps(data, indent=2))
    except Exception:
        pass


def load_token() -> Optional[dict]:
    """Load cached user token; return None if absent or expired with no refresh token."""
    if not TOKEN_CACHE.exists():
        return None
    try:
        data = json.loads(TOKEN_CACHE.read_text())
        # Return even if expired — let refresh_token_if_needed() handle it
        return data
    except Exception:
        return None


def load_valid_token() -> Optional[dict]:
    """Load token only if it is still valid (not expired)."""
    data = load_token()
    if data and data.get("expires_at", 0) > time.time() + 60:
        return data
    return None


async def refresh_token_if_needed(ccs_session: str = "", force: bool = False) -> Optional[dict]:
    """
    Silently refresh the access token using the stored refresh token.

    Like gcloud/az/PS cmdlets: if the access token is expired (or within
    30 min of expiry) and a valid refresh token exists, exchange it for a
    new access token transparently.

    Also re-establishes the ccs-session if the new token includes an id_token,
    since the ccs-session can expire independently of the access token.

    Args:
        ccs_session: Override the ccs-session cookie to send with the refresh request.
        force:       If True, skip the expiry check and always refresh (e.g. after a 401).

    Returns the updated token dict, or None if refresh is not possible.
    """
    data = load_token()
    if not data:
        return None

    # Token still has >60 s left — no refresh needed yet (unless forced)
    if not force and data.get("expires_at", 0) > time.time() + 60:
        return data

    refresh_token = data.get("refresh_token")
    if not refresh_token:
        return None

    # Use stored ccs_session if not passed in
    cookie = ccs_session or data.get("ccs_session", "")

    try:
        cookies = {"ccs-session": cookie} if cookie else {}
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.post(
                f"{SSO_URL}/as/token.oauth2",
                data={
                    "grant_type":    "refresh_token",
                    "refresh_token": refresh_token,
                    "client_id":     CLIENT_ID,
                },
                cookies=cookies or None,
            )
        if r.status_code != 200:
            return None

        new_token = r.json()
        access_token = new_token.get("access_token", "")
        id_token = new_token.get("id_token", "")

        # Re-establish ccs-session using new id_token if available
        if id_token and access_token:
            try:
                async with httpx.AsyncClient(timeout=15) as client:
                    rs = await client.post(
                        f"{USER_API_BASE}/authn/v1/session",
                        json={"id_token": id_token},
                        headers={
                            "Authorization": f"Bearer {access_token}",
                            "Accept": "application/json",
                            "Content-Type": "application/json",
                        },
                    )
                if rs.status_code == 200:
                    ws_id = data.get("workspace_id", "")
                    for c in client.cookies.jar:
                        if c.name == "ccs-session":
                            cookie = c.value
                            break
                    # Re-load workspace
                    if ws_id and cookie:
                        async with httpx.AsyncClient(timeout=15) as client:
                            await client.get(
                                f"{USER_API_BASE}/authn/v1/session/load-account/{ws_id}",
                                headers={"Authorization": f"Bearer {access_token}"},
                                cookies={"ccs-session": cookie},
                            )
                        # Re-check for updated ccs-session after load-account
                        for c in client.cookies.jar:
                            if c.name == "ccs-session":
                                cookie = c.value
                                break
            except Exception:
                pass  # ccs-session refresh is best-effort

        # Preserve workspace info and GLP credentials from the existing cache
        save_token(
            new_token,
            region=data.get("region", "us-west"),
            workspace_id=data.get("workspace_id", ""),
            workspace_name=data.get("workspace_name", ""),
            ccs_session=cookie,
            workspaces=data.get("workspaces", []),
            glp_client_id=data.get("glp_client_id", ""),
            glp_client_secret=data.get("glp_client_secret", ""),
            glp_credential_name=data.get("glp_credential_name", ""),
        )
        return load_token()

    except Exception:
        return None


def token_bearer() -> Optional[str]:
    """Return cached Bearer access token or None."""
    t = load_token()
    return t["access_token"] if t else None


# ---------------------------------------------------------------------------
# GLP API credential management (for global.api.greenlake.hpe.com)
# ---------------------------------------------------------------------------

GLP_CRED_NAME_PREFIX = "GLP-proliant-com-temp"
CREDENTIALS_URI = f"{USER_API_BASE}/authn/v1/token-management/credentials"
GLP_APP_INSTANCE_ID = "00000000-0000-0000-0000-000000000000"


_CLEANUP_PREFIXES = (
    GLP_CRED_NAME_PREFIX,        # current:  GLP-proliant-com-temp
    "GLP-hpecom-cli-temp",       # old hpecom tool
    "GLP-pcli-com-temp",         # old pcli tool
)


async def _cleanup_stale_proliant_credentials(access_token: str, ccs_session: str) -> None:
    """Delete any stale proliant credentials from previous sessions (all known name prefixes)."""
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/json",
    }
    cookies = {"ccs-session": ccs_session} if ccs_session else None
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.get(CREDENTIALS_URI, headers=headers, cookies=cookies)
            if r.status_code != 200:
                return
            items = r.json()
            if not isinstance(items, list):
                items = items.get("items", items.get("credentials", []))
            for item in items:
                name = item.get("credential_name", item.get("name", ""))
                if any(name.startswith(p) for p in _CLEANUP_PREFIXES):
                    await client.delete(
                        f"{CREDENTIALS_URI}/{name}",
                        headers=headers,
                        cookies=cookies,
                    )
    except Exception:
        pass


async def create_glp_api_credential(access_token: str, ccs_session: str, workspace_id: str) -> Optional[dict]:
    """Create a temporary GLP API client credential for global API access.

    Mirrors the PS module's Connect-HPEGLWorkspace step that creates
    a temporary credential for accessing global.api.greenlake.hpe.com.

    Payload requires credential_name + application_instance_id.
    For GLP-level (not service-specific) credentials, application_instance_id
    is the zero UUID ("00000000-0000-0000-0000-000000000000").

    Returns dict with 'client_id', 'client_secret', 'name', or None on failure.
    """
    import datetime

    # Clean up stale hpecom credentials from previous sessions first
    await _cleanup_stale_proliant_credentials(access_token, ccs_session)

    cred_name = f"{GLP_CRED_NAME_PREFIX}-{datetime.datetime.now().strftime('%y%m%d%H%M%S')}"
    payload = {
        "credential_name":        cred_name,
        "application_instance_id": GLP_APP_INSTANCE_ID,
    }
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.post(
                CREDENTIALS_URI,
                json=payload,
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                },
                cookies={"ccs-session": ccs_session} if ccs_session else None,
            )
        if r.status_code not in (200, 201):
            return None
        data = r.json()
        return {
            "client_id":     data.get("client_id", ""),
            "client_secret": data.get("client_secret", ""),
            "name":          cred_name,
        }
    except Exception:
        return None


async def delete_glp_api_credential(access_token: str, ccs_session: str, credential_name: str) -> bool:
    """Delete a temporary GLP API credential by name.

    Called on logout to clean up credentials created at login.
    URI format: DELETE /authn/v1/token-management/credentials/{name}
    """
    if not credential_name:
        return False
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.delete(
                f"{CREDENTIALS_URI}/{credential_name}",
                headers={"Authorization": f"Bearer {access_token}"},
                cookies={"ccs-session": ccs_session} if ccs_session else None,
            )
        return r.status_code in (200, 204)
    except Exception:
        return False


async def get_glp_api_token(client_id: str, client_secret: str, workspace_id: str) -> Optional[str]:
    """Obtain a GLP API access token via client_credentials grant.

    Uses the v1.2 workspace-scoped token endpoint when available,
    falling back to the standard SSO token endpoint.

    Returns the access_token string or None on failure.
    """
    token_url = f"{SSO_URL}/as/token.oauth2"
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.post(
                token_url,
                data={
                    "grant_type":    "client_credentials",
                    "client_id":     client_id,
                    "client_secret": client_secret,
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
        if r.status_code != 200:
            return None
        return r.json().get("access_token")
    except Exception:
        return None


async def switch_workspace(name_or_id: str) -> str:
    """Switch the active workspace by name or platform_customer_id.

    Re-uses the existing access token + ccs-session to call load-account,
    then updates token.json. No re-login required.
    """
    data = load_token()
    if not data:
        raise CredentialsError("Not logged in. Run 'proliant com login' first.")

    workspaces = data.get("workspaces", [])
    if not workspaces:
        raise CredentialsError(
            "No workspace list cached. Run 'proliant com login' to refresh."
        )

    # Match by exact name (case-insensitive), ID, or partial substring
    needle = name_or_id.lower()
    target = next(
        (w for w in workspaces
         if w.get("company_name", "").lower() == needle
         or w.get("platform_customer_id", "") == name_or_id
         or needle in w.get("company_name", "").lower()),
        None,
    )

    # Fall back to fuzzy match
    if not target:
        import difflib
        names = [w.get("company_name", "") for w in workspaces]
        close = difflib.get_close_matches(name_or_id, names, n=1, cutoff=0.6)
        if close:
            target = next(w for w in workspaces if w.get("company_name") == close[0])

    if not target:
        names = [w.get("company_name", "") for w in workspaces]
        raise ValueError(
            f"Workspace '{name_or_id}' not found.\n"
            f"Available workspaces: {', '.join(names)}\n"
            f"Run 'proliant com get workspaces' to see the full list."
        )

    new_ws_id   = target["platform_customer_id"]
    new_ws_name = target.get("company_name", "")

    access_token = data.get("access_token", "")
    ccs_session  = data.get("ccs_session", "")

    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.get(
            f"{USER_API_BASE}/authn/v1/session/load-account/{new_ws_id}",
            headers={"Authorization": f"Bearer {access_token}"},
            cookies={"ccs-session": ccs_session} if ccs_session else None,
        )
        if r.status_code not in (200, 204):
            raise AuthFlowError(
                f"Workspace switch failed ({r.status_code}): {r.text[:300]}"
            )

        # Pick up refreshed ccs-session if the server returned one
        for c in client.cookies.jar:
            if c.name == "ccs-session":
                ccs_session = c.value
                break

    # Update token.json in-place
    data["workspace_id"]   = new_ws_id
    data["workspace_name"] = new_ws_name
    data["ccs_session"]    = ccs_session
    TOKEN_CACHE.write_text(json.dumps(data, indent=2))
    TOKEN_CACHE.chmod(0o600)
    return new_ws_name  # return resolved name for display


# ---------------------------------------------------------------------------
# Main login entry point
# ---------------------------------------------------------------------------

class AuthFlowError(Exception):
    """Raised when the Okta IDX auth flow fails."""


async def okta_verify_login(email: str, region: str = "us-west") -> None:
    """
    Full Okta Verify push login for HPE GreenLake.

    Flow:
      1. PKCE authorize → stateToken at auth.hpe.com
      2. IDX identify (email)
      3. If corporate SSO: SAML → mylogin.hpe.com IDX
      4. Select Okta Verify push → display correctAnswer number
      5. Poll until push approved
      6. Follow redirect chain → auth code
      7. Exchange code for token → save to disk
    """
    last_error: Optional[Exception] = None

    for attempt in range(3):
        if attempt > 0:
            console.print(f"[dim]Retry {attempt}/2…[/dim]")
            await asyncio.sleep(2)

        try:
            verifier, challenge, state = _pkce()

            async with httpx.AsyncClient(follow_redirects=True, timeout=20) as client:
                # ── Step 1: stateToken ───────────────────────────────────────
                with console.status("[cyan]Connecting to HPE GreenLake…[/cyan]"):
                    state_token, okta_base = await _get_state_token(client, challenge, state, email)

                # ── Step 2: IDX introspect ───────────────────────────────────
                idx_data = await _idx_introspect(client, okta_base, state_token)
                if idx_data is None:
                    raise AuthFlowError("Session expired at auth.hpe.com (introspect)")

                # ── Step 3: Identify (email) ─────────────────────────────────
                identify = next(
                    (v for v in idx_data["remediation"]["value"] if v["name"] == "identify"),
                    None,
                )
                if not identify:
                    raise AuthFlowError("No 'identify' remediation available")

                r3 = await client.post(
                    identify["href"],
                    json={"identifier": email, "stateHandle": idx_data["stateHandle"]},
                    headers=IDX_HEADERS,
                )
                d3 = r3.json()
                if "remediation" not in d3:
                    raise AuthFlowError(f"Identify failed: {d3}")

                remediations = [v["name"] for v in d3["remediation"]["value"]]

                # ── Step 4: Handle redirect-idp (corporate SSO) ──────────────
                current_idx_data = d3
                current_okta_base = okta_base

                if "redirect-idp" in remediations:
                    redirect_idp = next(
                        v for v in d3["remediation"]["value"]
                        if v["name"] == "redirect-idp"
                    )
                    with console.status("[cyan]Following corporate SSO…[/cyan]"):
                        current_idx_data, current_okta_base = await _follow_saml_to_workforce(
                            client, redirect_idp
                        )
                    remediations2 = [v["name"] for v in current_idx_data.get("remediation", {}).get("value", [])]
                    if "select-authenticator-authenticate" not in remediations2:
                        raise AuthFlowError(
                            f"Expected select-authenticator-authenticate at workforce IdP. "
                            f"Got: {remediations2}"
                        )

                elif "select-authenticator-authenticate" not in remediations:
                    raise AuthFlowError(
                        f"Unexpected remediations after identify: {remediations}"
                    )

                # ── Step 5: Select Okta Verify push ──────────────────────────
                poll_href, state_handle, correct_answer = await _select_okta_verify_push(
                    client, current_okta_base, current_idx_data
                )

                # Display number challenge prominently
                console.print()
                if correct_answer is not None:
                    console.print(Panel(
                        f"[bold yellow]{correct_answer}[/bold yellow]",
                        title="[cyan]Open Okta Verify and tap this number[/cyan]",
                        expand=False,
                        border_style="cyan",
                    ))
                else:
                    console.print(
                        "[cyan]Push notification sent — approve it in Okta Verify.[/cyan]"
                    )
                console.print()

                # ── Step 6: Poll until approved ───────────────────────────────
                with console.status("[cyan]Waiting for Okta Verify approval…[/cyan]"):
                    success_href = await _poll_push(client, poll_href, state_handle)

                # ── Step 7: Extract auth code ─────────────────────────────────
                with console.status("[cyan]Completing authorization…[/cyan]"):
                    auth_code = await _extract_code_from_redirects(client, success_href)

                # ── Step 8: Token exchange ────────────────────────────────────
                with console.status("[cyan]Exchanging token…[/cyan]"):
                    token = await _exchange_token(verifier, state, auth_code)

                # ── Step 9: Workspace session and GLP credential ──────────────
                workspace_id, workspace_name, ccs_session = "", "", ""
                ws_list = []
                id_token = token.get("id_token", "")
                access_token = token.get("access_token", "")
                if id_token and access_token:
                    try:
                        with console.status("[cyan]Connecting to workspace…[/cyan]"):
                            ws_list, init_ccs = await _init_workspace_session(access_token, id_token)
                        ws = await _pick_workspace(ws_list)
                        workspace_id   = ws["platform_customer_id"]
                        workspace_name = ws.get("company_name", "")
                        with console.status(f"[cyan]Loading '{workspace_name}'…[/cyan]"):
                            ccs_session = await _load_workspace(access_token, workspace_id, init_ccs)
                    except AuthFlowError as e:
                        console.print(f"[yellow]Warning: workspace session setup failed: {e}[/yellow]")

                # Create temporary GLP API credential for global API access
                glp_client_id, glp_client_secret, glp_credential_name = "", "", ""
                if ccs_session and workspace_id:
                    try:
                        with console.status("[cyan]Creating API credential…[/cyan]"):
                            glp_cred = await create_glp_api_credential(access_token, ccs_session, workspace_id)
                        if glp_cred:
                            glp_client_id     = glp_cred["client_id"]
                            glp_client_secret = glp_cred["client_secret"]
                            glp_credential_name = glp_cred.get("name", "")
                    except Exception:
                        pass  # GLP credential creation is best-effort

                # ── Save ──────────────────────────────────────────────────────
                save_token(token, region, workspace_id=workspace_id,
                           workspace_name=workspace_name, ccs_session=ccs_session,
                           workspaces=ws_list,
                           glp_client_id=glp_client_id,
                           glp_client_secret=glp_client_secret,
                           glp_credential_name=glp_credential_name)
                console.print(
                    f"[bold green]✓ Logged in as {email}[/bold green]"
                )
                console.print(
                    f"[dim]Token saved to {TOKEN_CACHE}[/dim]"
                )
                return  # success

        except AuthFlowError as e:
            last_error = e
            console.print(f"[yellow]Auth flow error:[/yellow] {e}")
        except Exception as e:
            last_error = e
            console.print(f"[red]Unexpected error:[/red] {e}")

    # All attempts failed
    raise AuthFlowError(
        f"Login failed after 3 attempts. Last error: {last_error}"
    )


async def password_login(email: str, password: str, region: str = "us-west") -> None:
    """
    Username + password login for external (non-HPE-SSO) accounts, e.g. gmail.com.

    Flow:
      1-3. Same PKCE + IDX identify as okta_verify_login
      4. Submit password authenticator
      5. If MFA (Okta Verify push) required after password, handle it
      6. Follow redirect chain → auth code → token exchange → workspace session
    """
    last_error: Optional[Exception] = None

    for attempt in range(3):
        if attempt > 0:
            console.print(f"[dim]Retry {attempt}/2…[/dim]")
            await asyncio.sleep(2)

        try:
            verifier, challenge, state = _pkce()

            async with httpx.AsyncClient(follow_redirects=True, timeout=20) as client:
                # ── Step 1: stateToken ───────────────────────────────────────
                with console.status("[cyan]Connecting to HPE GreenLake…[/cyan]"):
                    state_token, okta_base = await _get_state_token(client, challenge, state, email)

                # ── Step 2: IDX introspect ───────────────────────────────────
                idx_data = await _idx_introspect(client, okta_base, state_token)
                if idx_data is None:
                    raise AuthFlowError("Session expired at auth.hpe.com (introspect)")

                # ── Step 3: Identify (email) ─────────────────────────────────
                identify = next(
                    (v for v in idx_data["remediation"]["value"] if v["name"] == "identify"),
                    None,
                )
                if not identify:
                    raise AuthFlowError("No 'identify' remediation available")

                r3 = await client.post(
                    identify["href"],
                    json={"identifier": email, "stateHandle": idx_data["stateHandle"]},
                    headers=IDX_HEADERS,
                )
                d3 = r3.json()
                if "remediation" not in d3:
                    raise AuthFlowError(f"Identify failed: {d3}")

                remediations = [v["name"] for v in d3["remediation"]["value"]]

                if "redirect-idp" in remediations:
                    redirect_idp_entry = next(
                        (v for v in d3["remediation"]["value"] if v["name"] == "redirect-idp"),
                        {}
                    )
                    # Follow redirect-idp to the second Okta org (auth.hpe.com for
                    # external accounts), then submit the password authenticator there.
                    with console.status("[cyan]Connecting to HPE authentication…[/cyan]"):
                        d3, okta_base = await _follow_saml_to_workforce(client, redirect_idp_entry)
                    remediations = [v["name"] for v in d3.get("remediation", {}).get("value", [])]
                    if "select-authenticator-authenticate" not in remediations:
                        idp_type = str(redirect_idp_entry.get("type", "")).upper()
                        idp_name = redirect_idp_entry.get("idp", {}).get("name", idp_type or "external IDP")
                        raise AuthFlowError(
                            f"Password authenticator not available at {idp_name} ({okta_base}). "
                            f"Available: {remediations}. "
                            "This account may require 'proliant com login' (Okta Verify)."
                        )

                if "select-authenticator-authenticate" not in remediations:
                    raise AuthFlowError(
                        f"Unexpected remediations after identify: {remediations}"
                    )

                # ── Step 4: Submit password ───────────────────────────────────
                with console.status("[cyan]Authenticating…[/cyan]"):
                    try:
                        success_href = await _password_authenticate(
                            client, okta_base, d3, password
                        )
                    except _MFARequired as mfa:
                        # Password accepted but MFA still needed — try Okta Verify push
                        console.print("[dim]Password accepted — MFA required.[/dim]")
                        poll_href, state_handle, correct_answer = await _select_okta_verify_push(
                            client, mfa.okta_base, mfa.idx_data
                        )
                        console.print()
                        if correct_answer is not None:
                            console.print(Panel(
                                f"[bold yellow]{correct_answer}[/bold yellow]",
                                title="[cyan]Open Okta Verify and tap this number[/cyan]",
                                expand=False,
                                border_style="cyan",
                            ))
                        else:
                            console.print("[cyan]Push notification sent — approve in Okta Verify.[/cyan]")
                        console.print()
                        with console.status("[cyan]Waiting for Okta Verify approval…[/cyan]"):
                            success_href = await _poll_push(client, poll_href, state_handle)

                # ── Step 5: Extract auth code ─────────────────────────────────
                with console.status("[cyan]Completing authorization…[/cyan]"):
                    auth_code = await _extract_code_from_redirects(client, success_href)

                # ── Step 6: Token exchange ────────────────────────────────────
                with console.status("[cyan]Exchanging token…[/cyan]"):
                    token = await _exchange_token(verifier, state, auth_code)

                # ── Step 7: Workspace session and GLP credential ─────────────
                workspace_id, workspace_name, ccs_session, ws_list = "", "", "", []
                id_token    = token.get("id_token", "")
                access_token = token.get("access_token", "")
                if id_token and access_token:
                    try:
                        with console.status("[cyan]Connecting to workspace…[/cyan]"):
                            ws_list, init_ccs = await _init_workspace_session(access_token, id_token)
                        ws = await _pick_workspace(ws_list)
                        workspace_id   = ws["platform_customer_id"]
                        workspace_name = ws.get("company_name", "")
                        with console.status(f"[cyan]Loading '{workspace_name}'…[/cyan]"):
                            ccs_session = await _load_workspace(access_token, workspace_id, init_ccs)
                    except AuthFlowError as e:
                        console.print(f"[yellow]Warning: workspace session setup failed: {e}[/yellow]")

                # Create temporary GLP API credential for global API access
                glp_client_id, glp_client_secret, glp_credential_name = "", "", ""
                if ccs_session and workspace_id:
                    try:
                        with console.status("[cyan]Creating API credential…[/cyan]"):
                            glp_cred = await create_glp_api_credential(access_token, ccs_session, workspace_id)
                        if glp_cred:
                            glp_client_id     = glp_cred["client_id"]
                            glp_client_secret = glp_cred["client_secret"]
                            glp_credential_name = glp_cred.get("name", "")
                    except Exception:
                        pass  # GLP credential creation is best-effort

                # ── Save ──────────────────────────────────────────────────────
                save_token(token, region, workspace_id=workspace_id,
                           workspace_name=workspace_name, ccs_session=ccs_session,
                           workspaces=ws_list,
                           glp_client_id=glp_client_id,
                           glp_client_secret=glp_client_secret,
                           glp_credential_name=glp_credential_name)
                console.print(f"[bold green]✓ Logged in as {email}[/bold green]")
                console.print(f"[dim]Token saved to {TOKEN_CACHE}[/dim]")
                return  # success

        except AuthFlowError as e:
            if os.environ.get("PROLIANT_DEBUG"):
                import traceback
                console.print("[dim]" + traceback.format_exc() + "[/dim]")
            # Don't retry IDP mismatch / wrong-flow errors — they won't change on retry
            if "not available" in str(e).lower() and "authenticator" in str(e).lower():
                raise
            last_error = e
            console.print(f"[yellow]Auth flow error:[/yellow] {e}")
        except Exception as e:
            if os.environ.get("PROLIANT_DEBUG"):
                import traceback
                console.print("[dim]" + traceback.format_exc() + "[/dim]")
            last_error = e
            console.print(f"[red]Unexpected error:[/red] {e}")

    raise AuthFlowError(
        f"Login failed after 3 attempts. Last error: {last_error}"
    )
